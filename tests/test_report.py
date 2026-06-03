"""Tests for the analyst-report generator (HTML + Markdown)."""
from __future__ import annotations

import qatar
import qscreen_report as rep


def _li(c, v, pv):
    return {"account_code": c, "label_verbatim": c, "value": v,
            "comparatives": [{"period_label": "2022", "value": pv}]}


def _qnb_filing():
    return {"metadata": {"symbol": "QNBK", "fiscal_year": 2023, "fiscal_period": "FY",
                         "currency": "QAR", "unit_scale": 1000000},
            "statements": [{"type": "balance_sheet", "verbatim_text": "x", "line_items": [
                _li("IS_NET_INCOME", 15502, 14347), _li("IS_NET_INTEREST", 27800, 25600),
                _li("BS_TOTAL_ASSETS", 1200000, 1150000), _li("BS_TOTAL_EQUITY", 105000, 100000),
                _li("BS_LOANS", 830000, 810000), _li("BS_CUSTOMER_DEPOSITS", 870000, 850000),
                _li("KPI_CAR", 19.2, 19.7), _li("KPI_NPL", 3.0, 2.9), _li("KPI_COST_INCOME", 22.5, 23.1)]}],
            "segments": [
                {"dimension": "geography", "name": "Turkey", "metrics": {"revenue": 8000, "net_profit": 600},
                 "comparatives": [{"period_label": "2022", "metrics": {"revenue": 11000, "net_profit": 1500}}]}]}


def test_build_report_has_all_sections():
    r = rep.build_report("QNBK", [_qnb_filing()], qatar.profile_for_year("QNBK", 2023),
                         assumptions={"growth": 0.06}, price=16.0, shares=9230)
    h = r["html"]
    for section in ["Qatar National Bank", "Company context", "Finansbank", "Key ratios",
                    "Red flags", "Segments", "Valuation (DCF)", "Sensitivity", "Multi-year"]:
        assert section in h, f"missing: {section}"
    assert "FX TRY" in h                      # Turkey segment flagged via the profile
    assert r["markdown"].startswith("# Qatar National Bank")
    assert r["valuation"]["valuation"]["model"] == "residual_income"


def test_report_is_self_contained_html():
    r = rep.build_report("QNBK", [_qnb_filing()], qatar.profile_for_year("QNBK", 2023))
    assert r["html"].startswith("<!doctype html>") and "<style>" in r["html"]


def test_report_embeds_trend_charts():
    # two years (current + comparative) → inline SVG bars + a sparkline column
    h = rep.build_report("QNBK", [_qnb_filing()], qatar.profile_for_year("QNBK", 2023))["html"]
    assert "<svg class='bars'" in h and "<svg class='spark'" in h
    assert "<th>Trend</th>" in h


def test_build_report_handles_thin_data():
    thin = {"metadata": {"symbol": "MERS", "fiscal_year": 2023, "fiscal_period": "FY", "currency": "QAR"},
            "statements": [{"type": "income_statement", "verbatim_text": "x",
                            "line_items": [_li("IS_REVENUE", 100, 90)]}]}
    r = rep.build_report("MERS", [thin], qatar.profile_for_year("MERS", 2023))
    assert r["html"].startswith("<!doctype html>")     # no crash, still a valid doc
    assert "Red flags" in r["html"]
