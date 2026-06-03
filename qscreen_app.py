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

The OpenRouter key is read from the tool's .env (same as the CLI) or the
OPENROUTER_API_KEY env var. No agent, no command line per filing. Upload is
opt-in: a button appears only when the server has INGEST_TOKEN set, and even
then nothing leaves your machine until you click it.
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import traceback
from types import SimpleNamespace
from pathlib import Path

# Reuse the exact, tested engine — do NOT reimplement any of it here.
import qscreen_ingest as engine
import qscreen_analyze

try:
    from flask import Flask, request, Response, send_file
except ImportError:
    sys.exit("Flask not installed. Run:  pip install flask pdfplumber requests")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024  # 64 MB upload cap

# ── QSE taxonomy + per-stock knowledge ───────────────────────────────────────
# The sector → sub-sector tree and the symbol map now live in the qatar/ package
# (the single source of truth, with per-stock temporal profiles). Each sub-sector
# still maps to one of the engine's 5 EXTRACTION archetypes, which drive the LLM's
# parsing hint (conventional_bank / islamic_bank / insurance / industrial / other).
import qatar

QSE_TAXONOMY = qatar.QSE_TAXONOMY
SUBSECTOR_TO_EXTRACTION = qatar.SUBSECTOR_TO_EXTRACTION
SYMBOL_SUBSECTOR = qatar.SYMBOL_SUBSECTOR


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
  a.dl { display: inline-block; margin-top: 14px; margin-right: 8px; padding: 10px 16px; background: #06c; color: #fff; border-radius: 8px; text-decoration: none; font-weight: 600; }
  a.up { background: #0b6; } a.up.busy { background: #999; pointer-events: none; }
  .muted { color: #888; font-weight: 400; font-size: 12px; }
  details.adv { margin-top: 14px; } summary { cursor: pointer; color: #06c; font-weight: 600; }
  .keyhint { background: #eef6ff; border: 1px solid #cfe3ff; border-radius: 8px; padding: 10px 12px; margin-top: 10px; font-size: 13px; line-height: 1.5; }
  .keyhint a { color: #06c; font-weight: 700; } .keyhint code { background: #dceaff; padding: 1px 5px; border-radius: 4px; }
  .seg { margin-top: 18px; } .seg h3 { font-size: 16px; margin: 8px 0; } .seg h4 { font-size: 13px; color: #555; text-transform: capitalize; margin: 12px 0 4px; }
  table.seg { width: 100%; border-collapse: collapse; font-size: 13px; }
  table.seg th, table.seg td { border-bottom: 1px solid #eee; padding: 5px 8px; text-align: right; }
  table.seg th:first-child, table.seg td:first-child { text-align: left; }
  table.seg th { color: #888; font-weight: 600; }
  .fx { background: #fde8c8; color: #a05a00; border-radius: 4px; padding: 0 5px; font-size: 11px; font-weight: 700; }
  .ev { color: #06c; cursor: help; }
  .neg { color: #c33; } .pos { color: #0a7; }
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
  <details class="adv" open><summary>Provider / model — need an API key? open this</summary>
    <div class="row">
      <div><label>AI Provider</label>
        <select name="provider" id="provider">
          <option value="">auto (use whichever key is set)</option>
          <option value="minimax">MiniMax</option>
          <option value="openrouter">OpenRouter</option>
          <option value="kimi">Kimi (Moonshot)</option>
          <option value="openai">OpenAI</option>
          <option value="anthropic">Claude (Anthropic)</option>
        </select>
      </div>
      <div><label>Model <span class="muted">(blank = provider default)</span></label>
        <input name="model" id="model" placeholder="default" autocomplete="off"></div>
    </div>
    <p class="keyhint" id="provkey"></p>
  </details>
  <button type="submit" id="go">Extract</button>
</form>
<div id="out"></div>
<script>
const SYMBOL_SUBSECTOR = __SYMBOL_MAP_JSON__;
const UPLOAD_ENABLED = __UPLOAD_ENABLED__;
const PROVIDER_INFO = __PROVIDER_INFO_JSON__;
const f = document.getElementById('f'), out = document.getElementById('out'), go = document.getElementById('go');
const provEl = document.getElementById('provider'), modelEl = document.getElementById('model'),
      provKey = document.getElementById('provkey');
function updateProvider() {
  const info = PROVIDER_INFO[provEl.value];
  if (info) {
    modelEl.placeholder = info.model || 'default';
    provKey.innerHTML = '🔑 Need a key for <b>' + info.label + '</b>? ' +
      '<a href="' + info.url + '" target="_blank" rel="noopener">Click here to get one &#8599;</a>' +
      ', then add <code>' + info.env + '=your-key</code> to the <code>.env</code> file next to the app and restart it.';
  } else {
    modelEl.placeholder = 'default';
    provKey.innerHTML = '🔑 You need ONE provider API key. Pick a provider above to get a sign-up link, ' +
      'then add it to the <code>.env</code> file next to the app (e.g. <code>MINIMAX_API_KEY=your-key</code>) and restart it.';
  }
}
if (provEl) { provEl.addEventListener('change', updateProvider); updateProvider(); }

function fmtNum(x){ return (x==null)?'—':Number(x).toLocaleString(); }
function fmtPct(x){ if(x==null) return '<span>—</span>'; const c=x<0?'neg':'pos'; return '<span class="'+c+'">'+(x*100).toFixed(0)+'%</span>'; }
function renderSegments(sa){
  if(!sa || !sa.dimensions || !Object.keys(sa.dimensions).length) return '';
  let h = '<div class="seg"><h3>Segment breakdown ('+(sa.reporting_currency||'')+')</h3>';
  for(const dim of Object.keys(sa.dimensions)){
    const d = sa.dimensions[dim];
    h += '<h4>by '+dim.replace('_',' ')+'</h4><table class="seg"><tr><th>Segment</th>'
       + '<th>Revenue</th><th>YoY</th><th>Share</th><th>Net profit</th><th>YoY</th></tr>';
    for(const r of d.segments){
      const m=r.metrics||{}, y=r.yoy||{}, s=r.share||{};
      const fx = r.fx_exposed ? ' <span class="fx" title="'+(r.fx_note||'')+'">FX '+(r.currency||'')+'</span>' : '';
      const ev = (r.events&&r.events.length) ? ' <span class="ev" title="'+r.events.join(' · ')+'">ⓘ</span>' : '';
      h += '<tr><td>'+r.name+fx+ev+'</td><td>'+fmtNum(m.revenue)+'</td><td>'+fmtPct(y.revenue)
         + '</td><td>'+fmtPct(s.revenue)+'</td><td>'+fmtNum(m.net_profit)+'</td><td>'+fmtPct(y.net_profit)+'</td></tr>';
    }
    h += '</table>';
  }
  return h + '</div>';
}
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
let lastBlob = null, lastName = 'filing.json', lastFiling = null;
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
      lastName = data.filename; lastFiling = data.filing;
      html += '\\n\\n<a class="dl" id="dl" href="#">⬇ Download ' + data.filename + '</a>';
      if (UPLOAD_ENABLED && !data.problems.length)
        html += '<a class="dl up" id="up" href="#">⬆ Upload to qscreen.app</a>';
      html += renderSegments(data.segments_analysis);
      out.innerHTML = html;
      document.getElementById('dl').onclick = (ev) => {
        ev.preventDefault();
        const url = URL.createObjectURL(lastBlob);
        const a = document.createElement('a'); a.href = url; a.download = lastName; a.click();
        URL.revokeObjectURL(url);
      };
      const up = document.getElementById('up');
      if (up) up.onclick = async (ev) => {
        ev.preventDefault();
        up.classList.add('busy'); up.textContent = '⬆ Uploading…';
        const note = document.createElement('div');
        try {
          const r = await fetch('/upload', { method: 'POST', headers: {'Content-Type':'application/json'},
                                             body: JSON.stringify({ filing: lastFiling }) });
          const d = await r.json();
          if (r.ok) { up.textContent = '✅ Uploaded to qscreen.app'; }
          else {
            up.classList.remove('busy'); up.textContent = '⬆ Retry upload';
            note.className = 'err';
            note.textContent = 'Upload failed: ' + (d.error || 'unknown') +
              (d.problems ? '\\n - ' + d.problems.join('\\n - ') : '');
            out.appendChild(note);
          }
        } catch (err) {
          up.classList.remove('busy'); up.textContent = '⬆ Retry upload';
          note.className = 'err'; note.textContent = 'Upload failed: ' + err; out.appendChild(note);
        }
      };
    }
  } catch (err) { out.innerHTML = '<span class="err">Request failed: ' + err + '</span>'; }
  go.disabled = false; go.textContent = 'Extract';
};
</script>
</body></html>"""


@app.route("/")
def index():
    upload_enabled = bool(os.getenv("INGEST_TOKEN"))
    provider_info = {name: {"label": cfg["label"], "model": cfg["default_model"],
                            "url": cfg["key_url"], "env": cfg["env"][0]}
                     for name, cfg in engine.PROVIDERS.items()}
    html = (PAGE
            .replace("__SUBSECTOR_OPTIONS__", _subsector_options_html())
            .replace("__SYMBOL_MAP_JSON__", json.dumps(SYMBOL_SUBSECTOR))
            .replace("__PROVIDER_INFO_JSON__", json.dumps(provider_info))
            .replace("__UPLOAD_ENABLED__", "true" if upload_enabled else "false"))
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
        provider = (request.form.get("provider") or "").strip() or None  # None → auto-detect
        model = (request.form.get("model") or "").strip() or None

        # Build the same args object the CLI uses; resolve_provider picks the
        # base URL / model / key (from the matching env var) and validates them.
        args = SimpleNamespace(
            symbol=symbol, sector=sector, year=int(year), period=period,
            provider=provider, base_url=None, model=model,
            max_tokens=16384, timeout=600, retries=4,
            pages_per_chunk=12, overlap=1, no_chunk=False,
            no_json_mode=False, llm_key=None,
        )
        cfg = engine.resolve_provider(args)   # raises SystemExit (caught below) if no provider/key
        args._profile = qatar.profile_for_year(symbol, int(year))  # company+year-aware prompting

        # Save the upload to a private temp file (not a predictable CWD path).
        fd, tmp_path = tempfile.mkstemp(suffix=".pdf", prefix="qscreen_upload_")
        os.close(fd)
        up.save(tmp_path)
        try:
            pages, sha = engine.pdf_to_pages(tmp_path)
        finally:
            Path(tmp_path).unlink(missing_ok=True)

        filing = engine.extract_filing(pages, args)
        filing.setdefault("metadata", {}).update({
            "symbol": symbol, "sector": sector, "sub_sector": subsector,
            "fiscal_year": int(year),
            "fiscal_period": period, "source_file": up.filename, "source_sha256": sha,
            "extracted_at": engine.datetime.now(engine.timezone.utc).isoformat(),
            "extractor": {"provider": cfg["name"], "model": cfg["model"]},
        })
        problems = engine.validate_filing(filing)
        seg_analysis = qscreen_analyze.analyze_segments(filing, args._profile)
        nseg = len(filing.get("segments", []))
        summary = (f"Extracted {len(filing.get('statements', []))} statements, "
                   f"{nseg} segments, {len(filing.get('notes', []))} notes, "
                   f"audit={filing.get('audit', {}).get('opinion_type')}.")
        if problems:
            summary += f" ({len(problems)} note(s) below — review before uploading.)"
        else:
            summary += " Clean — ready to upload to qscreen.app."

        return {
            "summary": summary,
            "problems": problems,
            "filing": filing,
            "segments_analysis": seg_analysis,
            "filename": f"{symbol}_{year}_{period}_filing.json",
        }
    except SystemExit as e:                       # provider/key/model config errors
        return {"error": str(e)}, 400
    except Exception as e:
        return {"error": str(e), "detail": traceback.format_exc()[-1500:]}, 500


@app.route("/segments", methods=["POST"])
def segments():
    """Re-run the segment breakdown for a filing JSON (uses the Qatar profile
    for FX/event annotations when the symbol+year resolve)."""
    payload = request.get_json(silent=True) or {}
    filing = payload.get("filing")
    if not isinstance(filing, dict):
        return {"error": "missing 'filing' object"}, 400
    meta = filing.get("metadata") or {}
    profile = qatar.profile_for_year(meta.get("symbol") or payload.get("symbol") or "",
                                     meta.get("fiscal_year") or payload.get("year"))
    return qscreen_analyze.analyze_segments(filing, profile)


@app.route("/upload", methods=["POST"])
def upload():
    """Opt-in upload of an already-extracted filing to qscreen.app.

    Only enabled when the server has INGEST_TOKEN set; the extract step never
    uploads on its own — the user clicks Upload explicitly. A non-conforming
    filing is rejected here too, mirroring the CLI's safety gate.
    """
    token = os.getenv("INGEST_TOKEN")
    if not token:
        return {"error": "No INGEST_TOKEN configured on the server; cannot upload."}, 400
    payload = request.get_json(silent=True) or {}
    filing = payload.get("filing")
    if not isinstance(filing, dict):
        return {"error": "missing 'filing' object"}, 400
    problems = engine.validate_filing(filing)
    if problems:
        return {"error": "filing is non-conforming; not uploading", "problems": problems}, 400
    args = SimpleNamespace(
        api_url=os.getenv("QSCREEN_API_URL", "http://localhost:3004"), token=token)
    try:
        resp = engine.upload_filing(filing, args)
        return {"ok": True, "response": resp}
    except Exception as e:
        return {"error": str(e)}, 502


def main() -> None:
    host = os.getenv("QSCREEN_APP_HOST", "127.0.0.1")
    port = int(os.getenv("QSCREEN_APP_PORT", "8765"))
    print(f"\n  QScreen Filing Ingestor — open  http://{host}:{port}  in your browser\n")
    app.run(host=host, port=port, debug=False)


if __name__ == "__main__":
    main()
