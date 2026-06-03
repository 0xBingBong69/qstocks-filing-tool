#!/usr/bin/env python3
"""
qscreen_app.py — local browser app for the QSE filing ingestor.

Run it on your laptop, open the page, drag in a PDF, fill four fields, click
Extract. It runs the SAME engine as qscreen_ingest.py (imported, not
re-implemented) and gives you a downloadable JSON report to upload to
qscreen.app. Nothing is auto-uploaded — you stay in control.

    pip install flask pdfplumber requests
    python3 qscreen_app.py
    # then open http://127.0.0.1:8765 in your browser

The OpenRouter key is read from scripts/.env (same as the CLI) or the
OPENROUTER_API_KEY env var. No agent, no command line per filing.
"""
from __future__ import annotations

import io
import json
import sys
import traceback
from types import SimpleNamespace
from pathlib import Path

# Reuse the exact, tested engine — do NOT reimplement any of it here.
import qscreen_ingest as engine

try:
    from flask import Flask, request, Response, send_file
except ImportError:
    sys.exit("Flask not installed. Run:  pip install flask pdfplumber requests")

app = Flask(__name__)

# ── QSE taxonomy ─────────────────────────────────────────────────────────────
# The full sector → sub-sector tree shown on qscreen.app (mirrors
# src/lib/qse-companies.ts QSE_SUBSECTORS). Each sub-sector maps to one of the
# engine's 5 EXTRACTION categories, which drive the LLM's parsing hint:
#   conventional_bank → interest income/expense, NIM
#   islamic_bank      → sukuk / profit-sharing / quasi-equity, NO interest
#   insurance         → gross/net premiums, claims, loss & combined ratios
#   industrial        → revenue / COGS / inventory  ← ONLY for businesses that
#                       actually have cost-of-goods and inventory
#   other             → neutral (no archetype hint) — correct for asset/service
#                       businesses (real estate, utilities, telecom, holdings,
#                       healthcare services) that have NO COGS/inventory, so
#                       they must not be told "report COGS/inventory".
# The rich sub-sector is always stored in metadata regardless of category.
QSE_TAXONOMY = {
    "Banks & Financial Services": [
        ("Commercial Bank", "conventional_bank"),
        ("Islamic Bank", "islamic_bank"),
        ("Brokerage", "other"),
        ("Joint Investment", "other"),
        ("Financial Holding", "other"),
        ("Islamic Financial Services", "islamic_bank"),
    ],
    "Insurance": [
        ("Conventional Insurance", "insurance"),
        ("Takaful Insurance", "insurance"),
        ("Reinsurance", "insurance"),
        ("Life & Medical Insurance", "insurance"),
    ],
    "Real Estate": [   # rental/NAV/occupancy businesses — no COGS/inventory
        ("Diversified Real Estate", "other"),
        ("Property Development", "other"),
        ("Real Estate Holding", "other"),
    ],
    "Industrials": [
        ("Petrochemicals", "industrial"),
        ("Aluminium", "industrial"),
        ("Utilities", "other"),               # regulated revenue, not COGS-driven
        ("Cement & Building Materials", "industrial"),
        ("Oil & Gas Services", "industrial"),
        ("Diversified Manufacturing", "industrial"),
        ("Industrial Holding", "other"),       # holding co — consolidates, no own COGS
        ("Diversified Conglomerate", "other"),
        ("Diversified Holding", "other"),
        ("Trading & Distribution", "industrial"),
    ],
    "Consumer Goods & Services": [
        ("Food & Beverages", "industrial"),
        ("Food Production", "industrial"),
        ("Supermarkets & Retail", "industrial"),
        ("Fuel Retail", "industrial"),
        ("Technology Distribution", "industrial"),
        ("Medical Devices", "industrial"),
        ("Healthcare Services", "other"),      # service revenue, no COGS/inventory
        ("Education", "other"),
        ("Media & Entertainment", "other"),
    ],
    "Telecom & Technology": [   # service revenue (ARPU/subscribers) — no inventory
        ("Telecom Operator", "other"),
        ("IT Services", "other"),
    ],
    "Transport": [   # freight/charter service revenue — no COGS/inventory
        ("Shipping & Marine", "other"),
        ("Warehousing & Logistics", "other"),
        ("LNG Shipping", "other"),
    ],
    "Energy": [("Energy", "industrial")],
    "Other": [("Other", "other")],
}

# sub-sector label -> extraction category
SUBSECTOR_TO_EXTRACTION = {
    sub: cat for group in QSE_TAXONOMY.values() for (sub, cat) in group
}

# symbol -> sub-sector label (mirrors QSE_SUBSECTORS in qse-companies.ts)
SYMBOL_SUBSECTOR = {
    "QNBK": "Commercial Bank", "CBQK": "Commercial Bank", "DHBK": "Commercial Bank",
    "ABQK": "Commercial Bank", "KCBK": "Commercial Bank",
    "QIBK": "Islamic Bank", "QIIK": "Islamic Bank", "MARK": "Islamic Bank",
    "DUBK": "Islamic Bank", "QFBQ": "Islamic Bank",
    "DBIS": "Brokerage", "QOIS": "Joint Investment", "NLCS": "Financial Holding",
    "IHGS": "Islamic Financial Services",
    "QATI": "Conventional Insurance", "DOHI": "Conventional Insurance",
    "QGRI": "Reinsurance", "QLMI": "Life & Medical Insurance",
    "AKHI": "Takaful Insurance", "QISI": "Takaful Insurance", "BEMA": "Takaful Insurance",
    "UDCD": "Diversified Real Estate", "BRES": "Property Development",
    "ERES": "Real Estate Holding", "MRDS": "Property Development",
    "IQCD": "Petrochemicals", "MPHC": "Petrochemicals", "QAMC": "Aluminium",
    "QEWS": "Utilities", "QNCD": "Cement & Building Materials", "GISS": "Oil & Gas Services",
    "QIMD": "Diversified Manufacturing", "QIGD": "Industrial Holding",
    "AHCS": "Diversified Conglomerate", "IGRD": "Diversified Holding",
    "MKDM": "Diversified Holding", "MHAR": "Industrial Holding", "SIIS": "Trading & Distribution",
    "ZHCD": "Food & Beverages", "WDAM": "Food Production", "MERS": "Supermarkets & Retail",
    "BLDN": "Food Production", "QFLS": "Fuel Retail", "MCCS": "Technology Distribution",
    "QGMD": "Medical Devices", "MCGS": "Healthcare Services", "FALH": "Education",
    "QCFS": "Media & Entertainment",
    "ORDS": "Telecom Operator", "VFQS": "Telecom Operator", "MEZA": "IT Services",
    "TQES": "IT Services",
    "QNNS": "Shipping & Marine", "GWCS": "Warehousing & Logistics", "QGTS": "LNG Shipping",
}


def _subsector_options_html() -> str:
    out = []
    for group, subs in QSE_TAXONOMY.items():
        out.append(f'<optgroup label="{group}">')
        for sub, _cat in subs:
            out.append(f'<option value="{sub}">{sub}</option>')
        out.append("</optgroup>")
    return "\n".join(out)

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>QScreen Filing Ingestor</title>
<style>
  body { font: 15px/1.5 system-ui, sans-serif; max-width: 720px; margin: 40px auto; padding: 0 16px; color: #1a1a1a; }
  h1 { font-size: 22px; } .sub { color: #666; margin-top: -8px; }
  label { display: block; margin: 14px 0 4px; font-weight: 600; }
  input, select { width: 100%; padding: 9px; border: 1px solid #ccc; border-radius: 6px; font-size: 15px; box-sizing: border-box; }
  .row { display: flex; gap: 12px; } .row > div { flex: 1; }
  button { margin-top: 20px; padding: 12px 20px; font-size: 16px; font-weight: 600; background: #0b6; color: #fff; border: 0; border-radius: 8px; cursor: pointer; }
  button:disabled { background: #999; cursor: wait; }
  #out { white-space: pre-wrap; background: #f6f6f6; border: 1px solid #e0e0e0; border-radius: 8px; padding: 14px; margin-top: 20px; display: none; }
  .ok { color: #0a7; } .warn { color: #c80; } .err { color: #c33; }
  .hint { color: #888; font-size: 13px; margin: 6px 0 0; min-height: 16px; }
  a.dl { display: inline-block; margin-top: 14px; padding: 10px 16px; background: #06c; color: #fff; border-radius: 8px; text-decoration: none; font-weight: 600; }
</style></head><body>
<h1>QScreen Filing Ingestor</h1>
<p class="sub">Drop a QSE financial-report PDF, fill the fields, click Extract. Then download the report and upload it to qscreen.app. Type a known symbol and the sub-sector auto-fills.</p>
<form id="f">
  <label>Filing PDF</label>
  <input type="file" name="pdf" accept="application/pdf" required>
  <div class="row">
    <div><label>Symbol</label><input name="symbol" id="symbol" placeholder="QIBK" autocomplete="off" required></div>
    <div><label>QSE Sector / Sub-sector</label>
      <select name="subsector" id="subsector" required>
        __SUBSECTOR_OPTIONS__
      </select>
    </div>
  </div>
  <p class="hint" id="hint"></p>
  <div class="row">
    <div><label>Year</label><input name="year" type="number" placeholder="2024" required></div>
    <div><label>Period</label>
      <select name="period">
        <option>FY</option><option>Q1</option><option>Q2</option><option>Q3</option>
        <option>Q4</option><option>H1</option><option>9M</option>
      </select>
    </div>
  </div>
  <button type="submit" id="go">Extract</button>
</form>
<div id="out"></div>
<script>
const SYMBOL_SUBSECTOR = __SYMBOL_MAP_JSON__;
const f = document.getElementById('f'), out = document.getElementById('out'), go = document.getElementById('go');
const symbolEl = document.getElementById('symbol'), subEl = document.getElementById('subsector'), hintEl = document.getElementById('hint');
symbolEl.addEventListener('input', () => {
  const sym = symbolEl.value.trim().toUpperCase().replace(/\\.QA$/, '');
  const sub = SYMBOL_SUBSECTOR[sym];
  if (sub) {
    subEl.value = sub;
    hintEl.textContent = sym + ' → ' + sub + ' (auto-filled; change if wrong)';
  } else {
    hintEl.textContent = sym ? (sym + ' not in the known list — pick the sub-sector manually') : '';
  }
});
let lastBlob = null, lastName = 'filing.json';
f.onsubmit = async (e) => {
  e.preventDefault();
  go.disabled = true; go.textContent = 'Extracting… (this can take a few minutes)';
  out.style.display = 'block'; out.textContent = 'Reading PDF and calling the model…';
  try {
    const res = await fetch('/extract', { method: 'POST', body: new FormData(f) });
    const data = await res.json();
    if (!res.ok) { out.innerHTML = '<span class="err">Error: ' + (data.error||'unknown') + '</span>\\n\\n' + (data.detail||''); }
    else {
      let html = '<span class="' + (data.problems.length ? 'warn' : 'ok') + '">' + data.summary + '</span>';
      if (data.problems.length) html += '\\n\\nNotes:\\n - ' + data.problems.join('\\n - ');
      lastBlob = new Blob([JSON.stringify(data.filing, null, 2)], {type:'application/json'});
      lastName = data.filename;
      html += '\\n\\n<a class="dl" id="dl" href="#">⬇ Download ' + data.filename + '</a>';
      out.innerHTML = html;
      document.getElementById('dl').onclick = (ev) => {
        ev.preventDefault();
        const url = URL.createObjectURL(lastBlob);
        const a = document.createElement('a'); a.href = url; a.download = lastName; a.click();
        URL.revokeObjectURL(url);
      };
    }
  } catch (err) { out.innerHTML = '<span class="err">Request failed: ' + err + '</span>'; }
  go.disabled = false; go.textContent = 'Extract';
};
</script>
</body></html>"""


@app.route("/")
def index():
    html = (PAGE
            .replace("__SUBSECTOR_OPTIONS__", _subsector_options_html())
            .replace("__SYMBOL_MAP_JSON__", json.dumps(SYMBOL_SUBSECTOR)))
    return Response(html, mimetype="text/html")


@app.route("/extract", methods=["POST"])
def extract():
    try:
        up = request.files.get("pdf")
        if not up:
            return {"error": "no PDF uploaded"}, 400
        symbol = (request.form.get("symbol") or "").strip().upper()
        subsector = (request.form.get("subsector") or "").strip()
        year = request.form.get("year")
        period = (request.form.get("period") or "FY").strip()
        if not (symbol and subsector and year):
            return {"error": "symbol, sub-sector and year are required"}, 400
        # The rich QSE sub-sector is stored; the extraction category (1 of 5)
        # drives how the LLM reads the statements.
        sector = SUBSECTOR_TO_EXTRACTION.get(subsector, "other")

        # Save the upload to a temp path the engine can open.
        tmp = Path.cwd() / f".upload_{symbol}_{year}_{period}.pdf"
        up.save(tmp)
        try:
            pages, sha = engine.pdf_to_pages(str(tmp))
        finally:
            tmp.unlink(missing_ok=True)

        key = (engine.os.getenv("LLM_API_KEY") or engine.os.getenv("OPENROUTER_API_KEY")
               or engine.os.getenv("MINIMAX_API_KEY"))
        if not key:
            return {"error": "No LLM key. Put OPENROUTER_API_KEY in scripts/.env or the environment."}, 400

        # Build the same args object the CLI uses, with sane defaults.
        args = SimpleNamespace(
            symbol=symbol, sector=sector, year=int(year), period=period,
            provider="openrouter", base_url=None, model=None,
            max_tokens=16384, timeout=600, retries=4,
            pages_per_chunk=12, overlap=1, no_chunk=False,
            no_json_mode=False, llm_key=key,
        )

        filing = engine.extract_filing(pages, args)
        filing.setdefault("metadata", {}).update({
            "symbol": symbol, "sector": sector, "sub_sector": subsector,
            "fiscal_year": int(year),
            "fiscal_period": period, "source_file": up.filename, "source_sha256": sha,
            "extracted_at": engine.datetime.now(engine.timezone.utc).isoformat(),
            "extractor": {"provider": "openrouter", "model": engine.DEFAULT_MODELS["openrouter"]},
        })
        problems = engine.validate_filing(filing)
        summary = (f"Extracted {len(filing.get('statements', []))} statements, "
                   f"{len(filing.get('notes', []))} notes, "
                   f"audit={filing.get('audit', {}).get('opinion_type')}.")
        if problems:
            summary += f" ({len(problems)} note(s) below — review before uploading.)"
        else:
            summary += " Clean — ready to upload to qscreen.app."

        return {
            "summary": summary,
            "problems": problems,
            "filing": filing,
            "filename": f"{symbol}_{year}_{period}_filing.json",
        }
    except Exception as e:
        return {"error": str(e), "detail": traceback.format_exc()[-1500:]}, 500


if __name__ == "__main__":
    print("\n  QScreen Filing Ingestor — open  http://127.0.0.1:8765  in your browser\n")
    app.run(host="127.0.0.1", port=8765, debug=False)
