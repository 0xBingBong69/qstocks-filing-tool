#!/usr/bin/env python3
"""qscreen_analyze.py — derived analysis over extracted QSE filings.

Phase 2 ships the segment analyzer: it turns a filing's typed `segments[]` into a
per-dimension breakdown with year-on-year growth, share-of-total, FX exposure
flags, and event annotations from the Qatar profile (e.g. "Turkey added via the
Finansbank acquisition, 2016"). Numbers are computed here; nothing is invented.

    from qscreen_analyze import analyze_segments
    out = analyze_segments(filing, profile)   # profile from qatar.profile_for_year

Later phases add compute_ratios / compute_trends / red_flags / DCF.
"""
from __future__ import annotations

import json
from pathlib import Path


def _num(x):
    if isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        return float(x)
    if isinstance(x, str):
        t = x.strip().replace(",", "").replace("(", "-").replace(")", "")
        try:
            return float(t)
        except ValueError:
            return None
    return None


def _yoy(cur, prior):
    c, p = _num(cur), _num(prior)
    if c is None or p is None or p == 0:
        return None
    return (c - p) / abs(p)


def _prior_metrics(sg: dict) -> dict:
    comps = sg.get("comparatives") or []
    if comps and isinstance(comps[0], dict):
        return comps[0].get("metrics") or {}
    return {}


def _segment_currency(sg: dict, profile: dict | None) -> str | None:
    if sg.get("currency"):
        return sg["currency"]
    if profile:
        name = (sg.get("name") or "").lower()
        subs = profile.get("active_subsidiaries") or profile.get("subsidiaries") or []
        for s in subs:
            country = (s.get("country") or "").lower()
            sname = (s.get("name") or "").lower()
            if (country and country in name) or (sname and sname in name):
                return s.get("currency")
    return None


def _segment_events(sg: dict, profile: dict | None) -> list[str]:
    if not profile:
        return []
    name = (sg.get("name") or "").lower()
    if not name:
        return []
    out = []
    for e in profile.get("active_events") or profile.get("events") or []:
        blob = f"{e.get('title', '')} {e.get('effect', '')}".lower()
        if name in blob:
            out.append(f"{e.get('year', '?')}: {e.get('title', '')}")
    return out


def analyze_segments(filing: dict, profile: dict | None = None) -> dict:
    """Per-dimension segment breakdown with YoY, share-of-total, FX flags and
    event annotations. Operates on one filing's segments[] (current vs the
    prior-year comparative each segment carries)."""
    meta = filing.get("metadata") or {}
    rep_ccy = (profile or {}).get("reporting_currency") or meta.get("currency") or "QAR"
    segments = filing.get("segments") or []

    by_dim: dict[str, list[dict]] = {}
    for sg in segments:
        if not (isinstance(sg, dict) and sg.get("name")):
            continue
        by_dim.setdefault(sg.get("dimension") or "business_line", []).append(sg)

    dimensions: dict[str, dict] = {}
    for dim, segs in by_dim.items():
        metric_keys = sorted({k for sg in segs for k in (sg.get("metrics") or {})})
        totals = {m: sum(v for v in (_num((sg.get("metrics") or {}).get(m)) for sg in segs)
                         if v is not None) for m in metric_keys}
        rows = []
        for sg in segs:
            cur = sg.get("metrics") or {}
            prior = _prior_metrics(sg)
            ccy = _segment_currency(sg, profile)
            fx_exposed = bool(ccy and ccy != rep_ccy)
            yoy = {m: _yoy(cur.get(m), prior.get(m)) for m in metric_keys if m in cur}
            share = {m: (_num(cur.get(m)) / totals[m]
                         if totals.get(m) not in (None, 0) and _num(cur.get(m)) is not None else None)
                     for m in metric_keys if m in cur}
            rows.append({
                "name": sg.get("name"), "currency": ccy, "fx_exposed": fx_exposed,
                "metrics": cur, "prior": prior, "yoy": yoy, "share": share,
                "fx_note": (f"Group reports in {rep_ccy}; this segment is denominated in "
                            f"{ccy} — its year-on-year change includes FX translation."
                            if fx_exposed else None),
                "events": _segment_events(sg, profile),
                "note_ref": sg.get("note_ref"),
            })
        rows.sort(key=lambda r: (_num((r["metrics"] or {}).get("revenue")) or 0), reverse=True)
        dimensions[dim] = {"total": totals, "metric_keys": metric_keys, "segments": rows}

    warnings = []
    if not segments:
        warnings.append("no segments[] in this filing — nothing to break down")
    return {
        "symbol": meta.get("symbol"), "fiscal_year": meta.get("fiscal_year"),
        "fiscal_period": meta.get("fiscal_period"), "reporting_currency": rep_ccy,
        "dimensions": dimensions, "warnings": warnings,
    }


def save_analysis(obj: dict, path: str) -> str:
    Path(path).write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")
    return path
