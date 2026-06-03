#!/usr/bin/env python3
"""qscreen_dcf.py — valuation / forecast simulator for QSE stocks.

Picks the right model for the company type (a bank's "free cash flow" is
ill-defined, so banks/insurers use an excess-return model, not FCF DCF):

  • non-financials (industrial / telecom / real estate / other) → FCFE DCF
  • banks & insurers                                            → Residual Income
                                                                  (+ DDM if dividends)

Every model returns a year-by-year projection, the PV split (explicit vs
terminal), an equity value and per-share value, plus a sensitivity grid over
growth × discount rate. Assumptions are seeded from the company's own history so
the defaults are realistic, and every input is overridable.

    from qscreen_dcf import value
    out = value("QNBK", filings, profile, assumptions={"discount_rate": 0.11})

CLI:
    python3 qscreen_dcf.py --symbol IQCD IQCD_2022_FY_filing.json IQCD_2023_FY_filing.json \
        --discount-rate 0.10 --growth 0.04 --terminal-growth 0.025 --shares 6050000000 --price 13.1
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from qscreen_series import build_series
import qscreen_analyze as analyze


# ── core models ──────────────────────────────────────────────────────────────

def _check(discount_rate: float, terminal_growth: float) -> None:
    if discount_rate <= 0:
        raise ValueError("discount_rate must be > 0")
    if terminal_growth >= discount_rate:
        raise ValueError("terminal_growth must be < discount_rate (Gordon terminal diverges otherwise)")


def _finish(model: str, projection: list[dict], pv_explicit: float, pv_terminal: float,
            assumptions: dict, shares, *, base_value: float = 0.0) -> dict:
    equity = base_value + pv_explicit + pv_terminal
    return {
        "model": model,
        "equity_value": equity,
        "per_share": (equity / shares if shares else None),
        "pv_explicit": pv_explicit,
        "pv_terminal": pv_terminal,
        "terminal_pct": (pv_terminal / equity if equity else None),
        "projection": projection,
        "assumptions": assumptions,
    }


def fcfe_dcf(base_fcf: float, *, discount_rate: float, growth: float,
             terminal_growth: float, years: int = 5, shares=None) -> dict:
    """Discount projected free cash flow to equity at the cost of equity."""
    _check(discount_rate, terminal_growth)
    r, g, gt = discount_rate, growth, terminal_growth
    proj, pv_explicit, cf = [], 0.0, base_fcf
    for t in range(1, years + 1):
        cf = base_fcf * (1 + g) ** t
        pv = cf / (1 + r) ** t
        pv_explicit += pv
        proj.append({"year": t, "cash_flow": cf, "pv": pv})
    tv = cf * (1 + gt) / (r - gt)
    pv_terminal = tv / (1 + r) ** years
    return _finish("fcfe_dcf", proj, pv_explicit, pv_terminal,
                   {"base_fcf": base_fcf, "discount_rate": r, "growth": g,
                    "terminal_growth": gt, "years": years}, shares)


def residual_income(book_equity: float, roe: float, *, discount_rate: float, growth: float,
                    terminal_growth: float, years: int = 5, shares=None) -> dict:
    """Excess-return (residual income) model: equity value = book value +
    PV of (ROE − cost of equity) × book, the right tool for banks/insurers."""
    _check(discount_rate, terminal_growth)
    r, g, gt = discount_rate, growth, terminal_growth
    proj, pv_ri, bv = [], 0.0, book_equity
    for t in range(1, years + 1):
        ri = (roe - r) * bv               # bv is the opening book value for year t
        pv = ri / (1 + r) ** t
        pv_ri += pv
        proj.append({"year": t, "opening_book": bv, "residual_income": ri, "pv": pv})
        bv = bv * (1 + g)
    # bv is now book value at year N, so (roe-r)*bv is exactly RI_{N+1}; the Gordon
    # continuing value is RI_{N+1}/(r-gt) — no extra (1+gt) (that would discount RI_{N+2}).
    ri_terminal = (roe - r) * bv
    tv = ri_terminal / (r - gt)
    pv_terminal = tv / (1 + r) ** years
    return _finish("residual_income", proj, pv_ri, pv_terminal,
                   {"book_equity": book_equity, "roe": roe, "discount_rate": r, "growth": g,
                    "terminal_growth": gt, "years": years}, shares, base_value=book_equity)


def ddm(dividend: float, *, discount_rate: float, growth: float, terminal_growth: float,
        years: int = 5, shares=None) -> dict:
    """Dividend discount model (Gordon). `dividend` is total dividends."""
    _check(discount_rate, terminal_growth)
    r, g, gt = discount_rate, growth, terminal_growth
    proj, pv_explicit, d = [], 0.0, dividend
    for t in range(1, years + 1):
        d = dividend * (1 + g) ** t
        pv = d / (1 + r) ** t
        pv_explicit += pv
        proj.append({"year": t, "dividend": d, "pv": pv})
    tv = d * (1 + gt) / (r - gt)
    pv_terminal = tv / (1 + r) ** years
    return _finish("ddm", proj, pv_explicit, pv_terminal,
                   {"dividend": dividend, "discount_rate": r, "growth": g,
                    "terminal_growth": gt, "years": years}, shares)


def sensitivity(model_fn, base_kwargs: dict, *, growth_values: list[float],
                rate_values: list[float], key: str = "per_share") -> dict:
    """2-D grid of the output `key` over growth × discount_rate."""
    grid = []
    for r in rate_values:
        row = []
        for g in growth_values:
            kw = dict(base_kwargs, growth=g, discount_rate=r)
            try:
                row.append(model_fn(**kw)[key])
            except (ValueError, ZeroDivisionError):
                row.append(None)
        grid.append(row)
    return {"growth_values": growth_values, "rate_values": rate_values, "grid": grid}


# ── seeding from history + orchestration ─────────────────────────────────────

def _latest_metrics(series: dict) -> dict:
    years = series.get("years") or {}
    return (years[sorted(years)[-1]].get("metrics") or {}) if years else {}


def _hist_growth(series: dict, code: str, default: float = 0.04) -> float:
    """A sensible default growth = historical CAGR of `code`, clamped to [0, 12%]."""
    t = analyze.compute_trends(series, "other").get(code) or {}
    cagr = t.get("cagr")
    if cagr is None:
        return default
    return max(0.0, min(0.12, cagr))


def value(symbol: str, filings: list[dict], profile: dict | None = None,
          assumptions: dict | None = None, *, price: float | None = None,
          shares: float | None = None) -> dict:
    """Pick the model by archetype, seed inputs from history, run it, and add a
    sensitivity grid (and upside vs price when price+shares are given)."""
    series = build_series(symbol, filings)
    archetype = (profile or {}).get("archetype") or "other"
    ratios = analyze.compute_ratios(series, archetype)
    m = _latest_metrics(series)
    a = dict(assumptions or {})
    r = a.get("discount_rate", 0.10)
    gt = a.get("terminal_growth", 0.025)
    years = int(a.get("years", 5))
    shares = shares if shares is not None else a.get("shares")
    warnings = list(series.get("warnings") or [])

    if archetype in ("conventional_bank", "islamic_bank", "insurance"):
        bv = analyze._g(m, "BS_TOTAL_EQUITY")
        latest_year = sorted(ratios)[-1] if ratios else None
        roe = analyze._val(ratios, latest_year, "roe") if latest_year else None
        g = a.get("growth", _hist_growth(series, "BS_TOTAL_EQUITY"))
        if bv is None or roe is None:
            return {"symbol": symbol.upper(), "archetype": archetype, "valuation": None,
                    "warnings": warnings + ["need book equity + ROE for a residual-income valuation"]}
        model_fn, base_kwargs = residual_income, {"book_equity": bv, "roe": roe,
                                                  "terminal_growth": gt, "years": years, "shares": shares}
        result = residual_income(bv, roe, discount_rate=r, growth=g, terminal_growth=gt,
                                 years=years, shares=shares)
    else:
        fcf = analyze._g(m, "CF_FCF")
        if fcf is None:
            ocf, capex = analyze._g(m, "CF_OCF"), analyze._g(m, "CF_CAPEX")
            # capex is an outflow regardless of the filing's sign convention.
            fcf = ocf - abs(capex) if (ocf is not None and capex is not None) else None
        g = a.get("growth", _hist_growth(series, "IS_REVENUE"))
        if fcf is None:
            return {"symbol": symbol.upper(), "archetype": archetype, "valuation": None,
                    "warnings": warnings + ["need free cash flow (CF_FCF or CF_OCF+CF_CAPEX) for a DCF"]}
        model_fn, base_kwargs = fcfe_dcf, {"base_fcf": fcf, "terminal_growth": gt,
                                           "years": years, "shares": shares}
        result = fcfe_dcf(fcf, discount_rate=r, growth=g, terminal_growth=gt,
                          years=years, shares=shares)

    grid = sensitivity(model_fn, base_kwargs,
                       growth_values=[round(g + d, 4) for d in (-0.02, -0.01, 0, 0.01, 0.02)],
                       rate_values=[round(r + d, 4) for d in (-0.02, -0.01, 0, 0.01, 0.02)],
                       key="per_share" if shares else "equity_value")

    upside = None
    if price not in (None, 0) and result.get("per_share"):
        upside = result["per_share"] / price - 1
    return {
        "symbol": symbol.upper(), "archetype": archetype, "reporting_currency": series.get("currency"),
        "valuation": result, "sensitivity": grid,
        "price": price, "upside": upside, "warnings": warnings,
    }


def save_valuation(obj: dict, path: str) -> str:
    Path(path).write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def main() -> int:
    p = argparse.ArgumentParser(description="DCF / valuation simulator for a QSE stock")
    p.add_argument("--symbol", required=True)
    p.add_argument("filings", nargs="+", help="SYMBOL_YEAR_PERIOD_filing.json files")
    p.add_argument("--discount-rate", type=float, default=0.10)
    p.add_argument("--terminal-growth", type=float, default=0.025)
    p.add_argument("--growth", type=float, help="override the history-seeded growth")
    p.add_argument("--years", type=int, default=5)
    p.add_argument("--shares", type=float)
    p.add_argument("--price", type=float, help="current share price, to show upside")
    args = p.parse_args()

    filings = [json.loads(Path(fp).read_text(encoding="utf-8")) for fp in args.filings]
    profile = None
    try:
        import qatar
        profile = qatar.profile_for_year(args.symbol, (filings[0].get("metadata") or {}).get("fiscal_year"))
    except Exception:
        pass
    a = {"discount_rate": args.discount_rate, "terminal_growth": args.terminal_growth,
         "years": args.years}
    if args.growth is not None:
        a["growth"] = args.growth
    out = value(args.symbol, filings, profile, a, price=args.price, shares=args.shares)
    path = save_valuation(out, f"{args.symbol.upper()}_valuation.json")
    v = out.get("valuation")
    if not v:
        print(f"⚠️  {out['symbol']}: {'; '.join(out['warnings'])}")
        return 1
    ps = f"{v['per_share']:.2f}/share" if v.get("per_share") else f"{v['equity_value']:,.0f} equity"
    up = f", upside {out['upside'] * 100:+.0f}% vs {out['price']}" if out.get("upside") is not None else ""
    print(f"💰 {out['symbol']} [{v['model']}] → {path}  ⇒ {ps}{up}  "
          f"(terminal {v['terminal_pct'] * 100:.0f}% of value)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
