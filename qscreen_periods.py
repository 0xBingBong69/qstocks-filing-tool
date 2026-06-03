#!/usr/bin/env python3
"""qscreen_periods.py — period-aware quarterly / trailing-twelve-month (TTM) roll-ups.

QSE interim filings report *flow* items (income statement, cash flow) as YTD
cumulative — Q1 = 3 months, H1/Q2 = 6, 9M/Q3 = 9, FY/Q4 = 12 — while balance-sheet
items are point-in-time. Given a set of filings for one company, this computes:

  • TTM flows   = latest YTD + prior FY − prior matching YTD   (a clean 12-month figure)
  • stocks      = the latest period's balance-sheet values      (point-in-time)
  • standalone quarter flows = consecutive YTD deltas           (e.g. 9M − H1 = Q3)

    from qscreen_periods import build_ttm
    ttm = build_ttm(filings)     # filings for one symbol; annual and/or interim

When the prior-year filings needed for a true TTM aren't supplied, it falls back to
the reported YTD and says so in `basis`/`warnings` — it never silently annualises.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from qscreen_series import _collect

PERIOD_MONTHS = {"FY": 12, "Q1": 3, "Q2": 6, "H1": 6, "9M": 9, "Q3": 9, "Q4": 12}


def _is_flow(code) -> bool:
    return isinstance(code, str) and code.startswith(("IS_", "CF_"))


def _is_stock(code) -> bool:
    return isinstance(code, str) and code.startswith("BS_")


def _num(x):
    if isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        return x
    if isinstance(x, str):
        t = x.strip().replace(",", "").replace("(", "-").replace(")", "")
        try:
            return float(t)
        except ValueError:
            return None
    return None


def filing_metrics(filing: dict) -> dict:
    """{code: numeric value} for the filing's current period."""
    cur, _, _ = _collect(filing)
    return {c: _num(v) for c, v in cur.items() if _num(v) is not None}


def _rows(filings: list[dict]) -> list[dict]:
    rows = []
    for f in filings:
        meta = f.get("metadata") or {}
        y = meta.get("fiscal_year")
        if y is None:
            continue
        p = meta.get("fiscal_period") or "FY"
        rows.append({"year": int(y), "period": p, "months": PERIOD_MONTHS.get(p, 12),
                     "metrics": filing_metrics(f), "source_file": meta.get("source_file")})
    rows.sort(key=lambda r: (r["year"], r["months"]))
    return rows


def _find(rows, year, months):
    for r in rows:
        if r["year"] == year and r["months"] == months:
            return r
    return None


def _prev_ytd(rows, latest):
    cands = [r for r in rows if r["year"] == latest["year"] and r["months"] < latest["months"]]
    return max(cands, key=lambda r: r["months"]) if cands else None


def build_ttm(filings: list[dict]) -> dict:
    """Trailing-twelve-month flows + point-in-time stocks for one company."""
    out = {"as_of": None, "basis": None, "ttm_months": 12, "flows": {}, "stocks": {},
           "ratios": {}, "standalone_quarter": None, "periods": [], "warnings": []}
    rows = _rows(filings)
    if not rows:
        out["warnings"].append("no filings with a fiscal_year")
        return out
    out["periods"] = [f"{r['period']} {r['year']}" for r in rows]

    latest = rows[-1]
    y, p, m, mx = latest["year"], latest["period"], latest["months"], latest["metrics"]
    out["as_of"] = f"{p} {y}"
    out["stocks"] = {c: v for c, v in mx.items() if _is_stock(c)}
    out["ratios"] = {c: v for c, v in mx.items() if isinstance(c, str) and c.startswith("KPI_")}

    if m == 12:
        out["flows"] = {c: v for c, v in mx.items() if _is_flow(c)}
        out["basis"] = f"FY {y} as reported"
    else:
        prior_fy, prior_int = _find(rows, y - 1, 12), _find(rows, y - 1, m)
        if prior_fy and prior_int:
            codes = {c for c in mx if _is_flow(c)} | {c for c in prior_fy["metrics"] if _is_flow(c)}
            flows = {}
            for c in codes:
                cur, pf, pi = mx.get(c), prior_fy["metrics"].get(c), prior_int["metrics"].get(c)
                if None not in (cur, pf, pi):
                    flows[c] = cur + pf - pi
            out["flows"] = flows
            out["basis"] = f"TTM = {p} {y} + FY {y - 1} − {p} {y - 1}"
        else:
            out["flows"] = {c: v for c, v in mx.items() if _is_flow(c)}
            out["ttm_months"] = m
            out["basis"] = f"{p} {y} YTD only ({m}m) — supply FY {y - 1} and {p} {y - 1} for a true TTM"
            out["warnings"].append(out["basis"])

    prev = _prev_ytd(rows, latest)
    if prev:
        sq = {c: mx[c] - prev["metrics"][c] for c in mx
              if _is_flow(c) and mx.get(c) is not None and prev["metrics"].get(c) is not None}
        out["standalone_quarter"] = {"label": f"{p} {y} standalone ({m - prev['months']}m)", "flows": sq}
    elif m == 3:
        out["standalone_quarter"] = {"label": f"{p} {y} standalone (3m)",
                                     "flows": {c: v for c, v in mx.items() if _is_flow(c)}}
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="TTM / quarterly roll-ups from filing JSONs (one company)")
    ap.add_argument("filings", nargs="+", help="*_filing.json files (annual and/or interim)")
    ap.add_argument("--json", action="store_true", help="emit the full result as JSON")
    a = ap.parse_args()
    ttm = build_ttm([json.loads(Path(p).read_text(encoding="utf-8")) for p in a.filings])
    if a.json:
        print(json.dumps(ttm, indent=2, ensure_ascii=False))
        return 0
    print(f"As of {ttm['as_of']} — {ttm['basis']}")
    print("Periods: " + ", ".join(ttm["periods"]))
    for c, v in sorted(ttm["flows"].items()):
        print(f"  {c:30} {v:>16,.0f}")
    if ttm["standalone_quarter"]:
        print(ttm["standalone_quarter"]["label"] + ":")
        for c, v in sorted(ttm["standalone_quarter"]["flows"].items()):
            print(f"  {c:30} {v:>16,.0f}")
    for w in ttm["warnings"]:
        print("⚠  " + w)
    return 0


if __name__ == "__main__":
    sys.exit(main())
