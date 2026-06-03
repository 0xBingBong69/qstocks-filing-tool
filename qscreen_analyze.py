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

import argparse
import json
import sys
from pathlib import Path
from types import SimpleNamespace

from qscreen_series import build_series


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


# ════════════════════════════════════════════════════════════════════════════
#  PHASE 3 — ratios, trends, red flags, analyst narrative
# ════════════════════════════════════════════════════════════════════════════

_BANK_INCOME = ["IS_NET_INTEREST", "IS_FEES_COMM", "IS_FX_GAIN", "IS_INVESTMENT_INCOME", "IS_OTHER_INCOME"]
_BANK_COST = ["IS_STAFF", "IS_OPERATING_EXP", "IS_DEPRECIATION"]


def _g(m, code):
    return _num((m or {}).get(code))


def _sum_present(m, codes):
    vals = [v for v in (_g(m, c) for c in codes) if v is not None]
    return sum(vals) if vals else None


def _avg(a, b):
    a, b = _num(a), _num(b)
    if a is None:
        return b
    if b is None:
        return a
    return (a + b) / 2.0


def _safe_div(n, d):
    n, d = _num(n), _num(d)
    if n is None or d in (None, 0):
        return None
    return n / d


def _kpi_pct(x):
    """A reported KPI ratio is printed as a percentage number (e.g. 1.3 for 1.3%,
    19.5 for 19.5%). Store it as a FRACTION so every ratio shares one unit and no
    magnitude-guessing is ever needed downstream."""
    x = _num(x)
    return x / 100.0 if x is not None else None


def _r(value, basis):
    return {"value": value, "basis": basis if value is not None else None}


def _kpi_or(cur, kpi, computed):
    """Prefer a printed KPI ratio (normalized percent→fraction), else the computed fraction."""
    k = _g(cur, kpi)
    return _r(_kpi_pct(k), "reported") if k is not None else _r(computed, "computed")


def _bank_ratios(cur, prior, islamic=False):
    eq = _avg(_g(cur, "BS_TOTAL_EQUITY"), _g(prior, "BS_TOTAL_EQUITY"))
    ta = _avg(_g(cur, "BS_TOTAL_ASSETS"), _g(prior, "BS_TOTAL_ASSETS"))
    ni = _g(cur, "IS_NET_INCOME")
    income, cost = _sum_present(cur, _BANK_INCOME), _sum_present(cur, _BANK_COST)
    out = {
        "roe": _r(_safe_div(ni, eq), "computed"),
        "roa": _r(_safe_div(ni, ta), "computed"),
        "cost_income": _kpi_or(cur, "KPI_COST_INCOME", _safe_div(cost, income)),
        "ldr": _kpi_or(cur, "KPI_LDR", _safe_div(_g(cur, "BS_LOANS"), _g(cur, "BS_CUSTOMER_DEPOSITS"))),
        "npl": _r(_kpi_pct(_g(cur, "KPI_NPL")), "reported"),
        "car": _r(_kpi_pct(_g(cur, "KPI_CAR")), "reported"),
        "coverage": _r(_kpi_pct(_g(cur, "KPI_COVERAGE")), "reported"),
    }
    if not islamic:
        out["nim"] = _kpi_or(cur, "KPI_NIM", _safe_div(_g(cur, "IS_NET_INTEREST"), ta))
    return out


def _insurance_ratios(cur, prior):
    eq = _avg(_g(cur, "BS_TOTAL_EQUITY"), _g(prior, "BS_TOTAL_EQUITY"))
    loss = _kpi_or(cur, "KPI_LOSS_RATIO", _safe_div(_g(cur, "IS_CLAIMS"), _g(cur, "IS_NET_PREMIUMS")))
    exp = _r(_kpi_pct(_g(cur, "KPI_EXPENSE_RATIO")), "reported")
    combined = _g(cur, "KPI_COMBINED")
    if combined is not None:
        combined_r = _r(_kpi_pct(combined), "reported")
    elif loss["value"] is not None and exp["value"] is not None:
        combined_r = _r(loss["value"] + exp["value"], "computed")   # both already fractions
    else:
        combined_r = _r(None, None)
    return {"loss_ratio": loss, "expense_ratio": exp, "combined_ratio": combined_r,
            "roe": _r(_safe_div(_g(cur, "IS_NET_INCOME"), eq), "computed")}


def _industrial_ratios(cur, prior):
    eq = _avg(_g(cur, "BS_TOTAL_EQUITY"), _g(prior, "BS_TOTAL_EQUITY"))
    ta = _avg(_g(cur, "BS_TOTAL_ASSETS"), _g(prior, "BS_TOTAL_ASSETS"))
    ni, rev = _g(cur, "IS_NET_INCOME"), _g(cur, "IS_REVENUE")
    reported_fcf = _g(cur, "CF_FCF")
    fcf = reported_fcf
    if fcf is None:
        ocf, capex = _g(cur, "CF_OCF"), _g(cur, "CF_CAPEX")
        # capex is an outflow regardless of the sign the filing uses for it.
        fcf = ocf - abs(capex) if (ocf is not None and capex is not None) else None
    div = _g(cur, "CF_DIVIDENDS_PAID")
    payout = _safe_div(abs(div), ni) if (div is not None and ni is not None and ni > 0) else None
    return {
        "net_margin": _r(_safe_div(ni, rev), "computed"),
        "operating_margin": _r(_safe_div(_g(cur, "IS_OPERATING_PROFIT"), rev), "computed"),
        "roe": _r(_safe_div(ni, eq), "computed"),
        "roa": _r(_safe_div(ni, ta), "computed"),
        "liabilities_to_equity": _r(_safe_div(_g(cur, "BS_TOTAL_LIABILITIES"),
                                              _g(cur, "BS_TOTAL_EQUITY")), "computed"),
        "fcf": _r(fcf, "reported" if reported_fcf is not None else "computed"),
        "dividend_payout": _r(payout, "computed"),
    }


def compute_ratios(series: dict, archetype: str) -> dict:
    """Per-year, sector-specific ratios. Prefers a printed KPI_* when present,
    otherwise computes from canonical codes; returns None (never a guess) when
    inputs are missing. `basis` records 'reported' vs 'computed'."""
    years = series.get("years") or {}
    out = {}
    for y in sorted(years):
        cur = years[y].get("metrics") or {}
        prior = (years.get(str(int(y) - 1)) or {}).get("metrics") or {}
        if archetype == "conventional_bank":
            out[y] = _bank_ratios(cur, prior, islamic=False)
        elif archetype == "islamic_bank":
            out[y] = _bank_ratios(cur, prior, islamic=True)
        elif archetype == "insurance":
            out[y] = _insurance_ratios(cur, prior)
        else:
            out[y] = _industrial_ratios(cur, prior)
    return out


_TREND_CODES = {
    "conventional_bank": ["IS_NET_INTEREST", "IS_NET_INCOME", "BS_TOTAL_ASSETS",
                          "BS_LOANS", "BS_CUSTOMER_DEPOSITS", "BS_TOTAL_EQUITY"],
    "islamic_bank": ["IS_NET_INCOME", "BS_TOTAL_ASSETS", "BS_LOANS",
                     "BS_CUSTOMER_DEPOSITS", "BS_TOTAL_EQUITY"],
    "insurance": ["IS_GROSS_PREMIUMS", "IS_NET_PREMIUMS", "IS_NET_INCOME", "BS_TOTAL_EQUITY"],
}
_DEFAULT_TREND = ["IS_REVENUE", "IS_OPERATING_PROFIT", "IS_NET_INCOME",
                  "BS_TOTAL_ASSETS", "BS_TOTAL_EQUITY", "CF_OCF"]


def compute_trends(series: dict, archetype: str) -> dict:
    """Per-metric YoY and CAGR across the available years."""
    years = series.get("years") or {}
    keys = sorted(years)
    out = {}
    for code in _TREND_CODES.get(archetype, _DEFAULT_TREND):
        pts = [(int(y), _num((years[y].get("metrics") or {}).get(code))) for y in keys]
        pts = [(y, v) for y, v in pts if v is not None]
        if not pts:
            continue
        latest_v = pts[-1][1]
        # Only call it YoY when the two latest present points are consecutive years.
        yoy = (_yoy(latest_v, pts[-2][1]) if len(pts) >= 2 and pts[-1][0] - pts[-2][0] == 1 else None)
        cagr = None
        span = pts[-1][0] - pts[0][0]
        if span > 0 and pts[0][1] and latest_v and pts[0][1] > 0 and latest_v > 0:
            cagr = (latest_v / pts[0][1]) ** (1.0 / span) - 1
        out[code] = {"latest": latest_v, "yoy": yoy, "cagr": cagr, "span_years": span,
                     "series": {str(y): v for y, v in pts}}
    return out


def _val(ratios, year, name):
    return ((ratios.get(year) or {}).get(name) or {}).get("value")


def red_flags(series: dict, ratios: dict, profile: dict | None = None,
              filings: list[dict] | None = None) -> list[dict]:
    """Rule-based warnings/alerts. Conservative: fires only on figures we have."""
    flags: list[dict] = []
    yrs = sorted(ratios or {})
    if yrs:
        # All ratio values are fractions (reported KPIs were normalized at source).
        cur, prev = yrs[-1], (yrs[-2] if len(yrs) >= 2 else None)
        car = _val(ratios, cur, "car")
        if car is not None and car < 0.13:
            flags.append({"severity": "alert", "rule": "low_car", "year": cur,
                          "message": f"Capital adequacy {car * 100:.1f}% is close to the Basel III minimum."})
        npl = _val(ratios, cur, "npl")
        if npl is not None and npl > 0.04:
            flags.append({"severity": "warn", "rule": "high_npl", "year": cur,
                          "message": f"NPL ratio {npl * 100:.1f}% is elevated."})
        nm = _val(ratios, cur, "net_margin")
        fcf = _val(ratios, cur, "fcf")
        if fcf is not None and fcf < 0:
            flags.append({"severity": "warn", "rule": "negative_fcf", "year": cur,
                          "message": "Free cash flow is negative."})
        cr = _val(ratios, cur, "combined_ratio")
        if cr is not None and cr > 1.0:
            flags.append({"severity": "alert", "rule": "underwriting_loss", "year": cur,
                          "message": f"Combined ratio {cr * 100:.0f}% exceeds 100% — underwriting loss."})
        if prev:
            npl_p = _val(ratios, prev, "npl")
            if npl is not None and npl_p is not None and npl - npl_p > 0.005:
                flags.append({"severity": "warn", "rule": "rising_npl", "year": cur,
                              "message": f"NPL ratio rose {(npl - npl_p) * 100:.1f}pp year-on-year."})
            ci, ci_p = _val(ratios, cur, "cost_income"), _val(ratios, prev, "cost_income")
            if ci is not None and ci_p is not None and ci - ci_p > 0.03:
                flags.append({"severity": "warn", "rule": "rising_cost_income", "year": cur,
                              "message": f"Cost-to-income rose to {ci * 100:.0f}%."})
            nm_p = _val(ratios, prev, "net_margin")
            if nm is not None and nm_p is not None and nm < nm_p - 0.03:
                flags.append({"severity": "warn", "rule": "margin_compression", "year": cur,
                              "message": f"Net margin fell to {nm * 100:.0f}% (from {nm_p * 100:.0f}%)."})

    sy = sorted((series.get("years") or {}))
    if len(sy) >= 2:
        eq_now = _g(series["years"][sy[-1]].get("metrics"), "BS_TOTAL_EQUITY")
        eq_prev = _g(series["years"][sy[-2]].get("metrics"), "BS_TOTAL_EQUITY")
        if eq_now is not None and eq_prev is not None and eq_now < eq_prev:
            flags.append({"severity": "warn", "rule": "equity_decline", "year": sy[-1],
                          "message": "Total equity declined year-on-year (possible FX translation effect)."})
    if series.get("restatements"):
        flags.append({"severity": "warn", "rule": "restatement", "year": None,
                      "message": f"{len(series['restatements'])} prior-year figure(s) were restated."})

    for f in filings or []:
        audit = f.get("audit") or {}
        fy = (f.get("metadata") or {}).get("fiscal_year")
        if (audit.get("material_uncertainty_going_concern") or {}).get("present"):
            flags.append({"severity": "alert", "rule": "going_concern", "year": fy,
                          "message": "Auditor flagged material uncertainty over going concern."})
        if audit.get("opinion_type") not in (None, "unknown", "unqualified"):
            flags.append({"severity": "alert", "rule": "audit_opinion", "year": fy,
                          "message": f"Audit opinion is '{audit.get('opinion_type')}' (not unqualified)."})
    return flags


def _narrative_args(args=None):
    base = dict(provider=None, base_url=None, model=None, llm_key=None,
                max_tokens=1500, timeout=120, retries=3, no_json_mode=True)
    if args is not None:
        for k in base:
            v = getattr(args, k, None)
            if v is not None:
                base[k] = v
    return SimpleNamespace(**base)


def analyst_narrative(analysis: dict, args=None) -> str:
    """Optional LLM pass: writes a Qatar-specialist commentary over the
    PRE-COMPUTED figures (the model narrates; it must not invent numbers)."""
    import qscreen_ingest as engine
    brief = {k: analysis.get(k) for k in ("symbol", "archetype", "reporting_currency",
                                          "ratios", "trends", "red_flags")}
    system = ("You are a senior equity analyst specialising in Qatar Stock Exchange (QSE) "
              "companies. You are given PRE-COMPUTED figures — never invent or recompute "
              "numbers; cite only what is provided. Write a concise plain-English analysis "
              "(5-8 sentences): the multi-year trend, profitability and key ratios versus the "
              "norm for this kind of QSE company, any red flags, and the segment / FX dynamics. "
              "Reference the company's known events (acquisitions, Basel III, FX) where relevant.")
    user = ("Company timeline & expectations:\n"
            + json.dumps(analysis.get("profile_context") or {}, ensure_ascii=False)[:2000]
            + "\n\nComputed figures:\n" + json.dumps(brief, ensure_ascii=False)[:6000])
    return engine.call_llm([{"role": "system", "content": system},
                            {"role": "user", "content": user}], _narrative_args(args))


def analyze(symbol: str, filings: list[dict], profile: dict | None = None, *,
            narrative: bool = False, args=None) -> dict:
    """Full analysis over a stock's filings: series → ratios → trends → red flags
    → segments (latest) → optional analyst narrative."""
    series = build_series(symbol, filings)
    archetype = (profile or {}).get("archetype") or "other"
    ratios = compute_ratios(series, archetype)
    trends = compute_trends(series, archetype)
    flags = red_flags(series, ratios, profile, filings)
    latest = (max(filings, key=lambda f: (f.get("metadata") or {}).get("fiscal_year") or 0)
              if filings else {})
    segs = analyze_segments(latest, profile)
    profile_context = None
    if profile:
        profile_context = {k: profile.get(k) for k in
                           ("ticker", "name_as_of", "archetype", "sub_sector",
                            "reporting_currency", "active_events", "watch_kpis")}
    out = {
        "symbol": symbol.upper(), "archetype": archetype,
        "reporting_currency": series.get("currency"),
        "years": sorted(series.get("years") or {}),
        "ratios": ratios, "trends": trends, "red_flags": flags,
        "segments": segs, "restatements": series.get("restatements") or [],
        "profile_context": profile_context, "warnings": series.get("warnings") or [],
    }
    if narrative:
        try:
            out["narrative"] = analyst_narrative(out, args)
        except SystemExit as ex:
            out["narrative_error"] = str(ex)
    return out


# ── peer comparison ──────────────────────────────────────────────────────────

# Ratios that matter per archetype, with the "good" direction for ranking.
_COMPARE_RATIOS = {
    "conventional_bank": [("roe", "high"), ("roa", "high"), ("nim", "high"),
                          ("cost_income", "low"), ("npl", "low"), ("car", "high"), ("ldr", None)],
    "islamic_bank": [("roe", "high"), ("roa", "high"), ("cost_income", "low"),
                     ("npl", "low"), ("car", "high"), ("ldr", None)],
    "insurance": [("roe", "high"), ("loss_ratio", "low"), ("combined_ratio", "low")],
    "industrial": [("net_margin", "high"), ("operating_margin", "high"), ("roe", "high"),
                   ("roa", "high"), ("liabilities_to_equity", "low")],
    "other": [("net_margin", "high"), ("operating_margin", "high"), ("roe", "high"),
              ("roa", "high"), ("liabilities_to_equity", "low")],
}


def compare(target: str, filings_by_symbol: dict, profiles_by_symbol: dict | None = None) -> dict:
    """Rank a stock against its peers on the ratios that matter for its type.
    `filings_by_symbol` is {SYMBOL: [filing dicts]} for the target and each peer;
    all are scored on the TARGET's archetype so the comparison is apples-to-apples."""
    profiles_by_symbol = profiles_by_symbol or {}
    target = target.upper()
    archetype = (profiles_by_symbol.get(target) or {}).get("archetype") or "other"
    specs = _COMPARE_RATIOS.get(archetype, _COMPARE_RATIOS["other"])
    names = [n for n, _ in specs]

    rows = []
    for sym, filings in filings_by_symbol.items():
        series = build_series(sym, filings)
        ratios = compute_ratios(series, archetype)
        latest = sorted(ratios)[-1] if ratios else None
        prof = profiles_by_symbol.get(sym) or {}
        rows.append({
            "symbol": sym.upper(),
            "name": prof.get("name_as_of") or prof.get("company_name") or sym.upper(),
            "year": latest,
            "ratios": {n: (_val(ratios, latest, n) if latest else None) for n in names},
            "is_target": sym.upper() == target,
        })

    ranks = {r["symbol"]: {} for r in rows}
    for name, direction in specs:
        if direction not in ("high", "low"):
            continue
        # All ratio values are fractions now, so they rank directly. Ties share a rank.
        present = [(r["symbol"], r["ratios"][name]) for r in rows if r["ratios"].get(name) is not None]
        ordered = sorted(present, key=lambda x: x[1], reverse=(direction == "high"))
        prev_val, prev_rank = object(), 0
        for i, (sym, val) in enumerate(ordered, 1):
            rank = prev_rank if val == prev_val else i
            ranks[sym][name] = rank
            prev_val, prev_rank = val, rank
    for r in rows:
        r["ranks"] = ranks[r["symbol"]]
    rows.sort(key=lambda r: (not r["is_target"], r["symbol"]))   # target first, then alphabetical
    return {"target": target, "archetype": archetype,
            "metrics": [{"name": n, "direction": d} for n, d in specs], "rows": rows}


def group_by_symbol(filings: list[dict]) -> dict:
    groups: dict[str, list[dict]] = {}
    for f in filings:
        sym = (f.get("metadata") or {}).get("symbol")
        if sym:
            groups.setdefault(sym.upper(), []).append(f)
    return groups


def save_analysis(obj: dict, path: str) -> str:
    Path(path).write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def _resolve_profiles(year_by_symbol: dict) -> dict:
    profs: dict = {}
    try:
        import qatar
        for sym, yr in year_by_symbol.items():
            profs[sym] = qatar.profile_for_year(sym, yr)
    except Exception:
        pass
    return profs


def main() -> int:
    p = argparse.ArgumentParser(description="Analyse a QSE stock's filings (ratios, trends, red flags)")
    p.add_argument("--symbol", help="ticker (the target for --compare; required otherwise)")
    p.add_argument("filings", nargs="+", help="SYMBOL_YEAR_PERIOD_filing.json files")
    p.add_argument("--compare", action="store_true",
                   help="rank a stock against peers (pass each company's files; grouped by symbol)")
    p.add_argument("--narrative", action="store_true", help="add an LLM analyst narrative (needs an API key)")
    p.add_argument("--provider")
    p.add_argument("--model")
    p.add_argument("--llm-key")
    args = p.parse_args()

    filings = [json.loads(Path(fp).read_text(encoding="utf-8")) for fp in args.filings]

    if args.compare:
        groups = group_by_symbol(filings)
        if not groups:
            print("⚠️  no filings carry a metadata.symbol to compare")
            return 1
        years = {s: (fs[0].get("metadata") or {}).get("fiscal_year") for s, fs in groups.items()}
        target = (args.symbol or next(iter(groups))).upper()
        out = compare(target, groups, _resolve_profiles(years))
        path = save_analysis(out, f"{target}_compare.json")
        names = [m["name"] for m in out["metrics"]]
        print(f"📊 {target} vs {len(out['rows']) - 1} peer(s) [{out['archetype']}] → {path}")
        print("   " + "TICKER".ljust(7) + "".join(n[:9].rjust(11) for n in names))
        for r in out["rows"]:
            cells = []
            for n in names:
                v, rk = r["ratios"].get(n), r["ranks"].get(n)
                cells.append((f"{v:.3g}" + (f"#{rk}" if rk else "")) if v is not None else "—")
            star = "★ " if r["is_target"] else "  "
            print(star + r["symbol"].ljust(7) + "".join(c.rjust(11) for c in cells))
        return 0

    if not args.symbol:
        p.error("--symbol is required for single-stock analysis (or use --compare)")
    profile = _resolve_profiles({args.symbol.upper(): (filings[0].get("metadata") or {}).get("fiscal_year")}
                                ).get(args.symbol.upper())
    out = analyze(args.symbol, filings, profile, narrative=args.narrative, args=args)
    path = save_analysis(out, f"{args.symbol.upper()}_analysis.json")
    print(f"🧮 {out['symbol']} [{out['archetype']}] → {path}  "
          f"({len(out['years'])} years, {len(out['red_flags'])} red flag(s))")
    for fl in out["red_flags"]:
        print(f"   {'🚨' if fl['severity'] == 'alert' else '⚠️ '} {fl['message']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
