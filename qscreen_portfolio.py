#!/usr/bin/env python3
"""qscreen_portfolio.py — screen and rank a whole basket of QSE stocks at once.

Given filings for several companies, this rolls each one through the analysis +
valuation engines and lays them out in a single ranked dashboard: latest-year
ROE / margin, year-on-year net-profit growth, red-flag counts, and the DCF value
(with upside when a price is supplied). Stocks are ranked healthiest-first
(fewest alerts, then highest ROE) — the screener the tool is named for.

    from qscreen_portfolio import roll_up, render_html
    board = roll_up({"QNBK": qnb_filings, "CBQK": cbq_filings}, profiles)

CLI:
    python3 qscreen_portfolio.py QNBK_2023_FY_filing.json CBQK_2023_FY_filing.json ORDS_2023_FY_filing.json
    # → watchlist.html + watchlist.json
"""
from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path

from qscreen_series import build_series
import qscreen_analyze as az
import qscreen_dcf as dcf


def _num(x):
    if x is None:
        return "—"
    try:
        x = float(x)
    except (TypeError, ValueError):
        return str(x)
    return f"{x:,.0f}" if abs(x) >= 100 else f"{x:,.2f}"


def _pct(x):
    return "—" if x is None else f"{x * 100:.1f}%"   # ratio values are fractions


def roll_up(filings_by_symbol: dict, profiles_by_symbol: dict | None = None,
            prices: dict | None = None, shares: dict | None = None) -> dict:
    """Roll each stock through analysis + valuation and rank the basket."""
    profiles_by_symbol = profiles_by_symbol or {}
    prices, shares = prices or {}, shares or {}
    rows = []
    for sym, filings in filings_by_symbol.items():
        sym = sym.upper()
        prof = profiles_by_symbol.get(sym)
        archetype = (prof or {}).get("archetype") or "other"
        series = build_series(sym, filings)
        ratios = az.compute_ratios(series, archetype)
        latest = sorted(ratios)[-1] if ratios else None
        flags = az.red_flags(series, ratios, prof, filings)
        alerts = sum(1 for f in flags if f["severity"] == "alert")
        trends = az.compute_trends(series, archetype)
        val = dcf.value(sym, filings, prof, {}, price=prices.get(sym), shares=shares.get(sym))
        v = val.get("valuation") or {}
        rows.append({
            "symbol": sym,
            "name": (prof or {}).get("name_as_of") or (prof or {}).get("company_name") or sym,
            "archetype": archetype, "year": latest, "currency": series.get("currency"),
            "roe": az._val(ratios, latest, "roe") if latest else None,
            "net_margin": az._val(ratios, latest, "net_margin") if latest else None,
            "ni_growth": (trends.get("IS_NET_INCOME") or {}).get("yoy"),
            "alerts": alerts, "warns": len(flags) - alerts,
            "model": v.get("model"), "per_share": v.get("per_share"),
            "equity_value": v.get("equity_value"), "upside": val.get("upside"),
        })
    # Healthiest first: fewest alerts, then highest ROE (None last).
    rows.sort(key=lambda r: (r["alerts"], -(r["roe"] if r["roe"] is not None else -99)))
    return {"count": len(rows), "rows": rows}


_CSS = """
body{font:14px/1.5 -apple-system,system-ui,sans-serif;color:#1a1a1a;max-width:960px;margin:24px auto;padding:0 18px}
h1{font-size:21px} table{width:100%;border-collapse:collapse;font-size:13px}
th,td{border-bottom:1px solid #eee;padding:6px 8px;text-align:right} th:first-child,td:first-child,td:nth-child(2){text-align:left}
th{color:#888} .pos{color:#0a7} .neg{color:#c33} .a{color:#c33;font-weight:700} .w{color:#b06b00}
.muted{color:#888;font-size:12px} tr:hover{background:#f7fbff}
"""


def render_html(board: dict) -> str:
    E = html.escape
    P = ["<h1>QSE watchlist — screened & ranked</h1>",
         f"<p class='muted'>{board['count']} stocks · healthiest first (fewest red-flag alerts, then ROE) · "
         "computed offline from filings</p>",
         "<table><tr><th>#</th><th>Ticker</th><th>Company</th><th>Type</th><th>Yr</th>"
         "<th>ROE</th><th>Net margin</th><th>NI YoY</th><th>Flags</th><th>DCF value</th><th>Upside</th></tr>"]
    for i, r in enumerate(board["rows"], 1):
        ccy = E(str(r.get("currency") or ""))      # filing-derived → escape
        flag = (f"<span class='a'>🚨{r['alerts']}</span> " if r["alerts"] else "") + \
               (f"<span class='w'>⚠️{r['warns']}</span>" if r["warns"] else "")
        val = (f"{ccy} {_num(r['per_share'])}/sh" if r.get("per_share")
               else (f"{ccy} {_num(r['equity_value'])}" if r.get("equity_value") else "—"))
        up = ("—" if r["upside"] is None
              else f"<span class='{'neg' if r['upside'] < 0 else 'pos'}'>{r['upside'] * 100:+.0f}%</span>")
        gj = r["ni_growth"]
        gjs = "—" if gj is None else f"<span class='{'neg' if gj < 0 else 'pos'}'>{gj * 100:+.0f}%</span>"
        P.append(f"<tr><td>{i}</td><td>{E(str(r['symbol']))}</td><td>{E(str(r['name']))}</td>"
                 f"<td class='muted'>{E(r['archetype'].replace('_', ' '))}</td><td>{r['year'] or '—'}</td>"
                 f"<td>{_pct(r['roe'])}</td><td>{_pct(r['net_margin'])}</td><td>{gjs}</td>"
                 f"<td>{flag or '—'}</td><td>{val}</td><td>{up}</td></tr>")
    P.append("</table><p class='muted'>Generated by QScreen — figures as reported or derived from "
             "filings, never invented.</p>")
    return ("<!doctype html><html><head><meta charset='utf-8'><title>QSE watchlist</title><style>"
            + _CSS + "</style></head><body>" + "".join(P) + "</body></html>")


def main() -> int:
    p = argparse.ArgumentParser(description="Screen & rank a basket of QSE stocks")
    p.add_argument("filings", nargs="+", help="filing JSONs for several companies (grouped by symbol)")
    p.add_argument("--out", default="watchlist", help="output basename (default: watchlist)")
    args = p.parse_args()

    filings = [json.loads(Path(fp).read_text(encoding="utf-8")) for fp in args.filings]
    groups = az.group_by_symbol(filings)
    if not groups:
        print("⚠️  no filings carry a metadata.symbol")
        return 1
    profiles = {}
    try:
        import qatar
        for sym, fs in groups.items():
            profiles[sym] = qatar.profile_for_year(sym, (fs[0].get("metadata") or {}).get("fiscal_year"))
    except Exception:
        pass
    board = roll_up(groups, profiles)
    Path(f"{args.out}.json").write_text(json.dumps(board, indent=2, ensure_ascii=False), encoding="utf-8")
    Path(f"{args.out}.html").write_text(render_html(board), encoding="utf-8")
    print(f"🗂️  Screened {board['count']} stock(s) → {args.out}.html + {args.out}.json")
    print("   " + "TICKER".ljust(7) + "ROE".rjust(8) + "NI YoY".rjust(9) + "FLAGS".rjust(8))
    for r in board["rows"]:
        roe = "—" if r["roe"] is None else f"{r['roe'] * 100:.1f}%"
        g = "—" if r["ni_growth"] is None else f"{r['ni_growth'] * 100:+.0f}%"
        print("   " + r["symbol"].ljust(7) + roe.rjust(8) + g.rjust(9)
              + f"{r['alerts']}A/{r['warns']}W".rjust(8))
    return 0


if __name__ == "__main__":
    sys.exit(main())
