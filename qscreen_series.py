#!/usr/bin/env python3
"""qscreen_series.py — stack many SYMBOL_YEAR_PERIOD_filing.json files into one
per-symbol multi-year time series, the input the analysis/DCF layers consume.

Each annual filing contributes its reported year PLUS the comparative column(s)
it prints, so even a single filing yields two years. When several filings cover
the same year, the as-originally-reported figures win and any later restatement
(the next year's comparative differing from the original) is recorded.

    from qscreen_series import build_series
    series = build_series("QNBK", [filing_2022, filing_2023])
    # series["years"]["2023"]["metrics"]["IS_NET_INCOME"] -> 15_502_000 ...

CLI:
    python3 qscreen_series.py --symbol QNBK QNBK_2022_FY_filing.json QNBK_2023_FY_filing.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_YEAR_RE = re.compile(r"(?:19|20)\d{2}")


def _year_from_label(label) -> int | None:
    if label is None:
        return None
    m = _YEAR_RE.search(str(label))
    return int(m.group(0)) if m else None


def _collect(filing: dict) -> tuple[dict, dict, dict]:
    """Return (current {code:value}, labels {code:label}, comps {year:{code:value}})."""
    cur: dict[str, object] = {}
    labels: dict[str, str] = {}
    comps: dict[int, dict] = {}
    for st in filing.get("statements") or []:
        for li in st.get("line_items") or []:
            code = li.get("account_code")
            if not code:
                continue
            if li.get("value") is not None:
                cur.setdefault(code, li["value"])
                labels.setdefault(code, li.get("label_verbatim"))
            for comp in li.get("comparatives") or []:
                if not isinstance(comp, dict):
                    continue
                py = _year_from_label(comp.get("period_label"))
                if py is not None and comp.get("value") is not None:
                    comps.setdefault(py, {}).setdefault(code, comp["value"])
    return cur, labels, comps


def _absorb(years: dict, restatements: list, year, source: str, meta: dict,
            metrics: dict, labels: dict) -> None:
    y = str(year)
    existing = years.get(y)
    if existing is None:
        years[y] = {
            "fiscal_year": year, "source": source,
            "source_file": meta.get("source_file"), "fiscal_period": meta.get("fiscal_period"),
            "metrics": dict(metrics),
            "labels": {k: v for k, v in labels.items() if k in metrics},
        }
        return
    if existing["source"] == "comparative" and source == "reported":
        # The originally-reported filing arrives — it wins; compare against the
        # later filing's comparative to surface restatements.
        for code, restated in existing["metrics"].items():
            original = metrics.get(code)
            if original is not None and restated is not None and original != restated:
                restatements.append({"year": year, "metric": code,
                                     "original": original, "restated": restated})
        existing.update(source="reported", source_file=meta.get("source_file"),
                        fiscal_period=meta.get("fiscal_period"))
        merged = dict(existing["metrics"])
        merged.update(metrics)               # reported values take precedence
        existing["metrics"] = merged
        existing["labels"].update({k: v for k, v in labels.items() if k in metrics})
    else:
        for code, val in metrics.items():    # only fill gaps
            existing["metrics"].setdefault(code, val)
            if code in labels:
                existing["labels"].setdefault(code, labels[code])


def build_series(symbol: str, filings: list[dict], *, annual_only: bool = True) -> dict:
    """Build a per-symbol multi-year series from extracted filing dicts."""
    sym = symbol.strip().upper()
    fs = [f for f in filings if (f.get("metadata") or {}).get("symbol", "").upper() == sym] or list(filings)
    if annual_only:
        fy = [f for f in fs if (f.get("metadata") or {}).get("fiscal_period") == "FY"]
        fs = fy or fs
    # Newest reported year first, so a reported year is seen before older filings' comparatives.
    fs = sorted(fs, key=lambda f: (f.get("metadata") or {}).get("fiscal_year") or 0, reverse=True)

    years: dict[str, dict] = {}
    restatements: list[dict] = []
    currency = unit_scale = None
    for f in fs:
        meta = f.get("metadata") or {}
        currency = currency or meta.get("currency")
        unit_scale = unit_scale or meta.get("unit_scale")
        cur, labels, comps = _collect(f)
        if meta.get("fiscal_year"):
            _absorb(years, restatements, meta["fiscal_year"], "reported", meta, cur, labels)
        for py, codes in comps.items():
            _absorb(years, restatements, py, "comparative", meta, codes, labels)

    codes = sorted({c for y in years.values() for c in y["metrics"]})
    return {
        "symbol": sym, "currency": currency, "unit_scale": unit_scale,
        "years": dict(sorted(years.items())),
        "codes": codes,
        "restatements": restatements,
        "warnings": [] if years else ["no annual data found in the provided filings"],
    }


def save_series(series: dict, path: str | None = None) -> str:
    out = path or f"{series['symbol']}_series.json"
    Path(out).write_text(json.dumps(series, indent=2, ensure_ascii=False), encoding="utf-8")
    return out


def main() -> int:
    p = argparse.ArgumentParser(description="Build a per-symbol multi-year series from filing JSONs")
    p.add_argument("--symbol", required=True)
    p.add_argument("filings", nargs="+", help="SYMBOL_YEAR_PERIOD_filing.json files")
    args = p.parse_args()
    filings = [json.loads(Path(fp).read_text(encoding="utf-8")) for fp in args.filings]
    series = build_series(args.symbol, filings)
    out = save_series(series)
    yrs = ", ".join(series["years"])
    print(f"📈 {series['symbol']} series → {out}  ({len(series['years'])} years: {yrs}; "
          f"{len(series['codes'])} metrics; {len(series['restatements'])} restatement(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
