"""Tests for the per-symbol multi-year series builder."""
from __future__ import annotations

import qscreen_series as s


def _filing(symbol, year, period, items):
    """items: list of (code, value, [(prior_label, prior_value), ...])."""
    line_items = []
    for code, value, comps in items:
        line_items.append({
            "account_code": code, "label_verbatim": code, "value": value,
            "comparatives": [{"period_label": pl, "value": pv} for pl, pv in comps],
        })
    return {"metadata": {"symbol": symbol, "fiscal_year": year, "fiscal_period": period,
                         "currency": "QAR", "unit_scale": 1000},
            "statements": [{"type": "income_statement", "verbatim_text": "x",
                            "line_items": line_items}]}


def test_year_from_label():
    assert s._year_from_label("2022") == 2022
    assert s._year_from_label("31 December 2023") == 2023
    assert s._year_from_label("year_ended_2024") == 2024
    assert s._year_from_label(None) is None
    assert s._year_from_label("n/a") is None


def test_single_filing_yields_two_years_via_comparatives():
    f = _filing("QNBK", 2023, "FY", [("IS_NET_INCOME", 15502, [("2022", 14347)])])
    series = s.build_series("QNBK", [f])
    assert set(series["years"]) == {"2022", "2023"}
    assert series["years"]["2022"]["source"] == "comparative"
    assert series["years"]["2022"]["metrics"]["IS_NET_INCOME"] == 14347


def test_reported_beats_comparative_and_flags_restatement():
    f23 = _filing("QNBK", 2023, "FY", [("IS_NET_INCOME", 15502, [("2022", 14347)])])
    f22 = _filing("QNBK", 2022, "FY", [("IS_NET_INCOME", 14349, [("2021", 13200)])])  # 14349 ≠ 14347
    series = s.build_series("QNBK", [f23, f22])
    assert set(series["years"]) == {"2021", "2022", "2023"}
    assert series["years"]["2022"]["source"] == "reported"
    assert series["years"]["2022"]["metrics"]["IS_NET_INCOME"] == 14349   # as-reported wins
    assert series["restatements"] == [
        {"year": 2022, "metric": "IS_NET_INCOME", "original": 14349, "restated": 14347}]


def test_annual_only_filters_interims_when_fy_present():
    fy = _filing("QNBK", 2023, "FY", [("BS_TOTAL_ASSETS", 1000, [])])
    q3 = _filing("QNBK", 2023, "Q3", [("BS_TOTAL_ASSETS", 950, [])])
    series = s.build_series("QNBK", [fy, q3])
    assert series["years"]["2023"]["fiscal_period"] == "FY"
    assert series["years"]["2023"]["metrics"]["BS_TOTAL_ASSETS"] == 1000


def test_currency_and_codes_surface():
    f = _filing("QNBK", 2023, "FY", [("IS_NET_INCOME", 1, []), ("BS_TOTAL_ASSETS", 2, [])])
    series = s.build_series("QNBK", [f])
    assert series["currency"] == "QAR" and series["unit_scale"] == 1000
    assert series["codes"] == ["BS_TOTAL_ASSETS", "IS_NET_INCOME"]


def test_empty_inputs_warn():
    series = s.build_series("QNBK", [])
    assert series["years"] == {} and series["warnings"]
