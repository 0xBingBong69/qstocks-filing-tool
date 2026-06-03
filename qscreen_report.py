#!/usr/bin/env python3
"""qscreen_report.py — one-page analyst report (HTML + Markdown) for a QSE stock.

Synthesises everything the tool computes — company context & event timeline,
multi-year figures, sector ratios, trends, red flags, the segment breakdown (with
FX and event annotations) and the DCF valuation with a sensitivity grid — into a
single shareable document. Numbers come from the filings; nothing is invented.

    from qscreen_report import build_report
    rep = build_report("QNBK", filings, profile, price=16.0, shares=9.2e9)
    open("QNBK_report.html", "w").write(rep["html"])

CLI:
    python3 qscreen_report.py --symbol QNBK QNBK_2022_FY_filing.json QNBK_2023_FY_filing.json \
        --price 16 --shares 9200000000
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

_PCT_RATIOS = {"roe", "roa", "nim", "cost_income", "npl", "car", "coverage", "ldr",
               "net_margin", "operating_margin", "loss_ratio", "expense_ratio",
               "combined_ratio", "dividend_payout"}

_CSS = """
body{font:14px/1.55 -apple-system,system-ui,sans-serif;color:#1a1a1a;max-width:820px;margin:24px auto;padding:0 18px}
h1{font-size:22px;margin:0 0 2px} h2{font-size:16px;border-bottom:2px solid #eee;padding-bottom:4px;margin:26px 0 8px}
h3{font-size:13px;color:#555;margin:14px 0 4px;text-transform:capitalize}
.sub{color:#555;margin:0 0 4px} .muted{color:#888;font-size:12px}
table{width:100%;border-collapse:collapse;font-size:13px;margin:6px 0}
th,td{border-bottom:1px solid #eee;padding:5px 8px;text-align:right} th:first-child,td:first-child{text-align:left}
th{color:#888;font-weight:600} .pos{color:#0a7} .neg{color:#c33}
.rep{color:#0a7;font-size:11px} .fx{background:#fde8c8;color:#a05a00;border-radius:4px;padding:0 5px;font-size:11px;font-weight:700}
ul.flags{list-style:none;padding:0;margin:6px 0} ul.flags li{padding:3px 0} li.alert{color:#c33;font-weight:600} li.warn{color:#b06b00}
ul.tl{list-style:none;padding-left:0} ul.tl li{border-left:3px solid #cfe3ff;padding:2px 0 2px 8px;margin:3px 0}
td.base{background:#fff3cd;font-weight:700} .tag{display:inline-block;background:#eef6ff;border:1px solid #cfe3ff;border-radius:6px;padding:1px 7px;font-size:11px;margin:0 4px 4px 0}
.hl{font-size:20px;font-weight:700;margin:4px 0}
"""


# ── formatting helpers ───────────────────────────────────────────────────────

def _num(x):
    if x is None:
        return "—"
    try:
        x = float(x)
    except (TypeError, ValueError):
        return str(x)
    return f"{x:,.0f}" if abs(x) >= 100 else f"{x:,.2f}"


def _pct(x):
    if x is None:
        return "—"
    return f"{x * 100:.1f}%"      # ratio values are fractions (KPIs normalized at source)


def _signed_pct(x):
    if x is None:
        return "—"
    p = x * 100
    return f"<span class='{'pos' if p >= 0 else 'neg'}'>{p:+.0f}%</span>"


def _code_label(code):
    return (code.replace("IS_", "").replace("BS_", "").replace("CF_", "")
            .replace("KPI_", "").replace("_", " ").title())


def _ratio_html(name, r):
    v = (r or {}).get("value")
    if v is None:
        return "—"
    rep = " <span class='rep'>®</span>" if r.get("basis") == "reported" else ""
    if name == "fcf":
        return _num(v) + rep
    if name == "liabilities_to_equity":
        return f"{v:.2f}×" + rep
    return _pct(v) + rep


# ── HTML sections ────────────────────────────────────────────────────────────

def _sensitivity_html(v, sg):
    per_share = v.get("per_share") is not None
    bg, br = v["assumptions"]["growth"], v["assumptions"]["discount_rate"]
    h = ["<h3>Sensitivity (" + ("per share" if per_share else "equity")
         + ") — growth → / discount ↓</h3><table><tr><th></th>"]
    h += [f"<th>{g * 100:.1f}%</th>" for g in sg["growth_values"]] + ["</tr>"]
    for i, r in enumerate(sg["rate_values"]):
        h.append(f"<tr><th>{r * 100:.1f}%</th>")
        for j, g in enumerate(sg["growth_values"]):
            cell = sg["grid"][i][j]
            base = abs(r - br) < 1e-9 and abs(g - bg) < 1e-9
            txt = "—" if cell is None else (f"{cell:,.2f}" if per_share else _num(cell))
            h.append(f"<td class='{'base' if base else ''}'>{txt}</td>")
        h.append("</tr>")
    return "".join(h) + "</table>"


def _series_table_html(series, archetype):
    years = sorted(series.get("years") or {})
    if not years:
        return ""
    codes = az._TREND_CODES.get(archetype, az._DEFAULT_TREND)
    present = [c for c in codes
               if any((series["years"][y].get("metrics") or {}).get(c) is not None for y in years)]
    if not present:
        return ""
    h = ["<table><tr><th>Metric</th>"] + [f"<th>{y}</th>" for y in years] + ["</tr>"]
    for c in present:
        h.append(f"<tr><td>{_code_label(c)}</td>")
        h += [f"<td>{_num((series['years'][y].get('metrics') or {}).get(c))}</td>" for y in years]
        h.append("</tr>")
    return "".join(h) + "</table>"


def _render_html(ctx, series, ratios, trends, flags, segments, valuation):
    E = html.escape
    yrs = ctx["years"]
    latest = yrs[-1] if yrs else None
    cur = E(str(ctx.get("currency") or ""))      # filing-derived → must be escaped
    sym = E(str(ctx.get("symbol") or ""))
    P = [f"<h1>{E(ctx['name'])} <span class='muted'>[{sym}]</span></h1>",
         f"<p class='sub'>{E(ctx.get('sub_sector') or '')} · {E(ctx['archetype'].replace('_', ' '))} · "
         f"reports in {cur or '—'} under {E(ctx.get('framework') or 'IFRS')}</p>"]
    if yrs:
        P.append(f"<p class='muted'>Analyst report · fiscal years {yrs[0]}–{yrs[-1]} · "
                 f"generated offline from filing data</p>")

    if ctx["events"] or ctx["subs"] or ctx["quirks"]:
        P.append("<h2>Company context</h2>")
        if ctx["subs"]:
            P.append("<p>Foreign operations: " + "".join(
                f"<span class='tag'>{E(s.get('name', ''))} · {E(s.get('country', ''))}/"
                f"{E(s.get('currency', ''))}</span>" for s in ctx["subs"]) + "</p>")
        if ctx["events"]:
            P.append("<ul class='tl'>")
            for e in sorted(ctx["events"], key=lambda e: e.get("year") or 0):
                P.append(f"<li><b>{e.get('year', '')}</b> — {E(e.get('title', ''))}. "
                         f"<span class='muted'>{E(e.get('effect', ''))}</span></li>")
            P.append("</ul>")
        if ctx["quirks"]:
            P.append("<p class='muted'>Accounting notes: " + "; ".join(E(q) for q in ctx["quirks"]) + "</p>")

    if latest and ratios.get(latest):
        P.append(f"<h2>Key ratios — {latest}</h2><table><tr><th>Ratio</th><th>Value</th></tr>")
        P += [f"<tr><td>{n.replace('_', ' ')}</td><td>{_ratio_html(n, r)}</td></tr>"
              for n, r in ratios[latest].items()]
        P.append("</table><p class='muted'>® = as reported by the company; others computed from the filing.</p>")

    if trends:
        P.append("<h2>Trends</h2><table><tr><th>Metric</th><th>Latest</th><th>YoY</th><th>CAGR</th></tr>")
        P += [f"<tr><td>{_code_label(c)}</td><td>{_num(t['latest'])}</td>"
              f"<td>{_signed_pct(t['yoy'])}</td><td>{_signed_pct(t['cagr'])}</td></tr>"
              for c, t in trends.items()]
        P.append("</table>")

    P.append("<h2>Red flags</h2>")
    if flags:
        P.append("<ul class='flags'>")
        P += [f"<li class='{'alert' if f['severity'] == 'alert' else 'warn'}'>"
              f"{'🚨' if f['severity'] == 'alert' else '⚠️'} {E(f['message'])}</li>" for f in flags]
        P.append("</ul>")
    else:
        P.append("<p class='muted'>None triggered.</p>")

    dims = (segments or {}).get("dimensions") or {}
    if dims:
        P.append("<h2>Segments</h2>")
        for dim, d in dims.items():
            P.append(f"<h3>by {dim.replace('_', ' ')}</h3><table>"
                     "<tr><th>Segment</th><th>Revenue</th><th>YoY</th><th>Share</th><th>Net profit</th></tr>")
            for r in d["segments"]:
                fx = f" <span class='fx'>FX {E(r.get('currency') or '')}</span>" if r.get("fx_exposed") else ""
                ev = (f" <span class='muted'>({E('; '.join(r.get('events') or []))})</span>"
                      if r.get("events") else "")
                m, y, s = r.get("metrics") or {}, r.get("yoy") or {}, r.get("share") or {}
                P.append(f"<tr><td>{E(str(r['name']))}{fx}{ev}</td><td>{_num(m.get('revenue'))}</td>"
                         f"<td>{_signed_pct(y.get('revenue'))}</td><td>{_pct(s.get('revenue'))}</td>"
                         f"<td>{_num(m.get('net_profit'))}</td></tr>")
            P.append("</table>")

    v = (valuation or {}).get("valuation")
    if v:
        P.append("<h2>Valuation (DCF)</h2>")
        head = (f"{cur} {_num(v['per_share'])} / share" if v.get("per_share")
                else f"{cur} {_num(v['equity_value'])} equity value")
        P.append(f"<p class='hl'>{head}</p>")
        up = valuation.get("upside")
        P.append(f"<p class='muted'>model: {v['model']} · terminal {v['terminal_pct'] * 100:.0f}% of value"
                 + (f" · upside <span class='{'neg' if up < 0 else 'pos'}'>{up * 100:+.0f}%</span> "
                    f"vs {valuation.get('price')}" if up is not None else "") + "</p>")
        if valuation.get("sensitivity"):
            P.append(_sensitivity_html(v, valuation["sensitivity"]))
    elif valuation and valuation.get("warnings"):
        P.append("<h2>Valuation (DCF)</h2><p class='muted'>" + E("; ".join(valuation["warnings"])) + "</p>")

    table = _series_table_html(series, ctx["archetype"])
    if table:
        P.append("<h2>Multi-year figures</h2>" + table)
    P.append("<p class='muted'>Generated by QScreen — computed offline from filing data; "
             "figures are as reported or derived from the filings, never invented.</p>")
    return ("<!doctype html><html><head><meta charset='utf-8'><title>"
            + E(ctx["name"]) + " — analyst report</title><style>" + _CSS + "</style></head><body>"
            + "".join(P) + "</body></html>")


def _render_md(ctx, series, ratios, trends, flags, segments, valuation):
    yrs = ctx["years"]
    latest = yrs[-1] if yrs else None
    L = [f"# {ctx['name']} [{ctx['symbol']}]",
         f"_{ctx.get('sub_sector') or ''} · {ctx['archetype'].replace('_', ' ')} · "
         f"{ctx.get('currency') or ''} · {ctx.get('framework') or 'IFRS'}_", ""]
    if ctx["events"]:
        L.append("## Company context")
        L += [f"- **{e.get('year', '')}** — {e.get('title', '')}. {e.get('effect', '')}"
              for e in sorted(ctx["events"], key=lambda e: e.get("year") or 0)]
        L.append("")
    if latest and ratios.get(latest):
        L += [f"## Key ratios — {latest}", "", "| Ratio | Value |", "|---|---|"]
        for n, r in ratios[latest].items():
            v = r.get("value")
            cell = "—" if v is None else (_num(v) if n in ("fcf",) else
                                          (f"{v:.2f}×" if n == "liabilities_to_equity" else _pct(v)))
            if r.get("basis") == "reported" and v is not None:
                cell += " (reported)"
            L.append(f"| {n.replace('_', ' ')} | {cell} |")
        L.append("")
    L.append("## Red flags")
    L += ([f"- {'🚨' if f['severity'] == 'alert' else '⚠️'} {f['message']}" for f in flags]
          if flags else ["_None triggered._"])
    L.append("")
    v = (valuation or {}).get("valuation")
    if v:
        head = (f"{ctx.get('currency') or ''} {_num(v['per_share'])} / share" if v.get("per_share")
                else f"{ctx.get('currency') or ''} {_num(v['equity_value'])} equity value")
        up = valuation.get("upside")
        L += ["## Valuation (DCF)", f"**{head}** — model {v['model']}, terminal "
              f"{v['terminal_pct'] * 100:.0f}% of value"
              + (f", upside {up * 100:+.0f}% vs {valuation.get('price')}" if up is not None else ""), ""]
    L.append("_Generated by QScreen — computed offline from filing data; never invented._")
    return "\n".join(L)


# ── orchestration ────────────────────────────────────────────────────────────

def build_report(symbol: str, filings: list[dict], profile: dict | None = None, *,
                 assumptions: dict | None = None, price: float | None = None,
                 shares: float | None = None) -> dict:
    symbol = symbol.upper()
    series = build_series(symbol, filings)
    archetype = (profile or {}).get("archetype") or "other"
    ratios = az.compute_ratios(series, archetype)
    trends = az.compute_trends(series, archetype)
    flags = az.red_flags(series, ratios, profile, filings)
    latest_filing = (max(filings, key=lambda f: (f.get("metadata") or {}).get("fiscal_year") or 0)
                     if filings else {})
    segments = az.analyze_segments(latest_filing, profile)
    valuation = dcf.value(symbol, filings, profile, assumptions or {}, price=price, shares=shares)
    ctx = {
        "symbol": symbol, "archetype": archetype, "currency": series.get("currency"),
        "name": (profile or {}).get("name_as_of") or (profile or {}).get("company_name") or symbol,
        "sub_sector": (profile or {}).get("sub_sector"),
        "framework": ((profile or {}).get("framework_as_of")
                      or ((profile or {}).get("framework_timeline") or [{}])[0].get("framework")),
        "years": sorted(series.get("years") or {}),
        "events": (profile or {}).get("active_events") or (profile or {}).get("events") or [],
        "subs": (profile or {}).get("active_subsidiaries") or (profile or {}).get("subsidiaries") or [],
        "quirks": (profile or {}).get("accounting_quirks") or [],
    }
    return {
        "symbol": symbol,
        "html": _render_html(ctx, series, ratios, trends, flags, segments, valuation),
        "markdown": _render_md(ctx, series, ratios, trends, flags, segments, valuation),
        "analysis": {"ratios": ratios, "trends": trends, "red_flags": flags, "segments": segments},
        "valuation": valuation,
    }


def main() -> int:
    p = argparse.ArgumentParser(description="Generate a one-page analyst report for a QSE stock")
    p.add_argument("--symbol", required=True)
    p.add_argument("filings", nargs="+", help="SYMBOL_YEAR_PERIOD_filing.json files")
    p.add_argument("--price", type=float)
    p.add_argument("--shares", type=float)
    p.add_argument("--discount-rate", type=float, default=0.10)
    p.add_argument("--growth", type=float)
    p.add_argument("--terminal-growth", type=float, default=0.025)
    p.add_argument("--years", type=int, default=5)
    args = p.parse_args()

    filings = [json.loads(Path(fp).read_text(encoding="utf-8")) for fp in args.filings]
    profile = None
    try:
        import qatar
        profile = qatar.profile_for_year(args.symbol, (filings[0].get("metadata") or {}).get("fiscal_year"))
    except Exception:
        pass
    a = {"discount_rate": args.discount_rate, "terminal_growth": args.terminal_growth, "years": args.years}
    if args.growth is not None:
        a["growth"] = args.growth
    rep = build_report(args.symbol, filings, profile, assumptions=a, price=args.price, shares=args.shares)
    base = args.symbol.upper()
    Path(f"{base}_report.html").write_text(rep["html"], encoding="utf-8")
    Path(f"{base}_report.md").write_text(rep["markdown"], encoding="utf-8")
    print(f"📰 {base} analyst report → {base}_report.html  +  {base}_report.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
