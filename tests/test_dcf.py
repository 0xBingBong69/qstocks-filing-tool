"""Tests for the DCF / valuation simulator (Phase 4)."""
from __future__ import annotations

import math

import pytest

import qatar
import qscreen_dcf as d


def _filing(symbol, year, items):
    li = [{"account_code": c, "label_verbatim": c, "value": v,
           "comparatives": [{"period_label": str(year - 1), "value": pv}]} for c, v, pv in items]
    return {"metadata": {"symbol": symbol, "fiscal_year": year, "fiscal_period": "FY", "currency": "QAR"},
            "statements": [{"type": "balance_sheet", "verbatim_text": "x", "line_items": li}]}


def test_fcfe_dcf_golden():
    f = d.fcfe_dcf(100, discount_rate=0.10, growth=0.05, terminal_growth=0.025, years=5, shares=10)
    assert math.isclose(f["equity_value"], 1518.86, abs_tol=0.5)
    assert math.isclose(f["per_share"], f["equity_value"] / 10)
    assert 0 < f["terminal_pct"] < 1 and len(f["projection"]) == 5


def test_residual_income_golden_and_book_anchor():
    ri = d.residual_income(1000, 0.15, discount_rate=0.10, growth=0.05, terminal_growth=0.025, years=5)
    # Gordon continuing value uses RI_{N+1}/(r-gt) (no spurious extra (1+gt) growth step).
    assert math.isclose(ri["equity_value"], 1735.84, abs_tol=0.5)
    # The key sanity check: when ROE == cost of equity, value collapses to book.
    flat = d.residual_income(1000, 0.10, discount_rate=0.10, growth=0.05, terminal_growth=0.025, years=5)
    assert math.isclose(flat["equity_value"], 1000.0, abs_tol=1e-6)
    # ROE below cost of equity destroys value (trades below book).
    bad = d.residual_income(1000, 0.06, discount_rate=0.10, growth=0.05, terminal_growth=0.025, years=5)
    assert bad["equity_value"] < 1000


def test_ddm_basic():
    v = d.ddm(50, discount_rate=0.10, growth=0.04, terminal_growth=0.025, years=5, shares=100)
    assert v["equity_value"] > 0 and v["per_share"] == v["equity_value"] / 100


def test_terminal_growth_guard():
    with pytest.raises(ValueError):
        d.fcfe_dcf(100, discount_rate=0.05, growth=0.03, terminal_growth=0.06, years=5)


def test_sensitivity_grid():
    s = d.sensitivity(d.fcfe_dcf, {"base_fcf": 100, "terminal_growth": 0.025, "years": 5, "shares": 10},
                      growth_values=[0.03, 0.05, 0.07], rate_values=[0.08, 0.10, 0.12], key="per_share")
    assert len(s["grid"]) == 3 and all(len(row) == 3 for row in s["grid"])
    # higher growth ⇒ higher value; higher discount ⇒ lower value
    assert s["grid"][1][2] > s["grid"][1][0]
    assert s["grid"][0][1] > s["grid"][2][1]


def test_value_bank_uses_residual_income():
    f = _filing("QNBK", 2023, [("IS_NET_INCOME", 15000, 13000), ("BS_TOTAL_EQUITY", 100000, 95000)])
    out = d.value("QNBK", [f], qatar.profile_for_year("QNBK", 2023),
                  {"discount_rate": 0.10, "terminal_growth": 0.025, "growth": 0.05}, shares=1000)
    assert out["valuation"]["model"] == "residual_income"
    assert out["valuation"]["per_share"] is not None
    assert len(out["sensitivity"]["grid"]) == 5


def test_value_industrial_uses_fcfe_with_upside():
    f = _filing("IQCD", 2023, [("IS_REVENUE", 18000, 16000), ("CF_OCF", 6000, 5000), ("CF_CAPEX", -1000, -900)])
    out = d.value("IQCD", [f], qatar.profile_for_year("IQCD", 2023),
                  {"discount_rate": 0.10, "terminal_growth": 0.025, "growth": 0.04}, price=10.0, shares=6050)
    assert out["valuation"]["model"] == "fcfe_dcf" and out["upside"] is not None


def test_value_missing_inputs_warns():
    f = _filing("QNBK", 2023, [("IS_REVENUE", 1, 1)])
    out = d.value("QNBK", [f], qatar.profile_for_year("QNBK", 2023))
    assert out["valuation"] is None and out["warnings"]
