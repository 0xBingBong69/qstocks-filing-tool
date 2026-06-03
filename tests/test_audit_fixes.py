"""Regression tests for the audit-fix batch (one per finding)."""
from __future__ import annotations

import math

import pytest

import qatar
import qscreen_analyze as az
import qscreen_dcf as d
import qscreen_series as s
import qscreen_ingest as e
import qscreen_report as rep
import qscreen_portfolio as pf


def _bankf(sym, **codes):
    li = [{"account_code": c, "label_verbatim": c, "value": v,
           "comparatives": [{"period_label": "2022", "value": v}]} for c, v in codes.items()]
    return {"metadata": {"symbol": sym, "fiscal_year": 2023, "fiscal_period": "FY", "currency": "QAR"},
            "statements": [{"type": "income_statement", "verbatim_text": "x", "line_items": li}]}


def _xss_filing(sym="X", currency="QAR"):
    return {"metadata": {"symbol": sym, "fiscal_year": 2023, "fiscal_period": "FY", "currency": currency},
            "statements": [{"type": "income_statement", "verbatim_text": "x", "line_items": [
                {"account_code": "IS_NET_INCOME", "label_verbatim": "x", "value": 1,
                 "comparatives": [{"period_label": "2022", "value": 1}]}]}]}


# ── P0 ───────────────────────────────────────────────────────────────────────

def test_residual_income_terminal_drops_spurious_growth_step():
    def correct(bv0, roe, r, g, gt, N):
        bv, pv = bv0, 0.0
        for t in range(1, N + 1):
            pv += (roe - r) * bv / (1 + r) ** t
            bv *= (1 + g)
        return bv0 + pv + ((roe - r) * bv / (r - gt)) / (1 + r) ** N
    ri = d.residual_income(1000, 0.15, discount_rate=0.10, growth=0.04, terminal_growth=0.025, years=5)
    assert math.isclose(ri["equity_value"], correct(1000, 0.15, 0.10, 0.04, 0.025, 5))


def test_reported_kpi_stored_as_fraction():
    r = az.compute_ratios({"years": {"2023": {"metrics": {"KPI_CAR": 19.5, "KPI_NPL": 1.3}}}},
                          "conventional_bank")["2023"]
    assert math.isclose(r["car"]["value"], 0.195) and math.isclose(r["npl"]["value"], 0.013)


def test_low_npl_not_flagged_but_high_npl_is():
    low = {"years": {"2023": {"metrics": {"KPI_NPL": 1.3, "BS_TOTAL_EQUITY": 100}}}}
    high = {"years": {"2023": {"metrics": {"KPI_NPL": 5.0, "BS_TOTAL_EQUITY": 100}}}}
    assert not any(f["rule"] == "high_npl" for f in az.red_flags(low, az.compute_ratios(low, "conventional_bank")))
    assert any(f["rule"] == "high_npl" for f in az.red_flags(high, az.compute_ratios(high, "conventional_bank")))


def test_combined_ratio_160_percent_flags_underwriting_loss():
    ins = {"years": {"2023": {"metrics": {"KPI_COMBINED": 160.0, "BS_TOTAL_EQUITY": 100, "IS_NET_INCOME": -5}}}}
    assert any(f["rule"] == "underwriting_loss" for f in az.red_flags(ins, az.compute_ratios(ins, "insurance")))


def test_dcf_price_zero_does_not_crash():
    out = d.value("QNBK", [_bankf("QNBK", IS_NET_INCOME=15000, BS_TOTAL_EQUITY=100000)],
                  qatar.profile_for_year("QNBK", 2023), {"growth": 0.05}, price=0, shares=1000)
    assert out["upside"] is None and out["valuation"]["per_share"] is not None


def test_flatten_tolerates_dict_comparatives():
    f = {"statements": [{"type": "income_statement", "line_items": [
        {"account_code": "IS_REVENUE", "label_verbatim": "Rev", "value": 10,
         "comparatives": {"period_label": "2022", "value": 8}}]}]}   # dict, not list
    rows = e.flatten_line_items(f)                                    # must not raise
    assert rows[0]["prior_value"] is None


# ── P1 ───────────────────────────────────────────────────────────────────────

def test_fcf_treats_capex_as_outflow_regardless_of_sign():
    def fcf(capex):
        m = {"CF_OCF": 300, "CF_CAPEX": capex, "IS_REVENUE": 1, "IS_NET_INCOME": 1, "BS_TOTAL_EQUITY": 1}
        return az.compute_ratios({"years": {"2023": {"metrics": m}}}, "industrial")["2023"]["fcf"]["value"]
    assert fcf(-120) == 180 and fcf(120) == 180


def test_series_refuses_to_mix_companies():
    out = s.build_series("XXX", [{"metadata": {"symbol": "YYY", "fiscal_year": 2023, "fiscal_period": "FY"},
                                  "statements": []}])
    assert out["years"] == {} and any("refusing to mix" in w for w in out["warnings"])


def test_series_warns_when_only_interim():
    f = {"metadata": {"symbol": "X", "fiscal_year": 2023, "fiscal_period": "Q3"},
         "statements": [{"type": "x", "line_items": [
             {"account_code": "IS_REVENUE", "label_verbatim": "r", "value": 5}]}]}
    assert any("interim" in w for w in s.build_series("X", [f])["warnings"])


def test_trends_yoy_null_when_years_not_consecutive():
    t = az.compute_trends({"years": {"2020": {"metrics": {"IS_NET_INCOME": 100}},
                                     "2023": {"metrics": {"IS_NET_INCOME": 144}}}},
                          "conventional_bank")["IS_NET_INCOME"]
    assert t["yoy"] is None and t["cagr"] is not None


def test_validate_requires_comparative_value():
    f = e.empty_filing()
    f["metadata"].update(symbol="Q", fiscal_year=2023, fiscal_period="FY")
    f["audit"]["opinion_type"] = "unqualified"
    f["statements"] = [{"type": "income_statement", "title": "t", "period_label": "2023", "verbatim_text": "x",
                        "line_items": [{"account_code": "IS_REVENUE", "label_verbatim": "Rev", "value": 10,
                                        "comparatives": [{"period_label": "2022"}]}]}]   # label, no value
    assert any("comparatives" in p for p in e.validate_filing(f))


def test_report_escapes_filing_derived_currency():
    # Matching symbol so the series is built; the filing's currency is attacker-controlled.
    h = rep.build_report("QNBK", [_xss_filing(sym="QNBK", currency="<script>x</script>")], None)["html"]
    assert "<script>x</script>" not in h and "&lt;script&gt;" in h


def test_portfolio_escapes_symbol():
    evil = "<script>y</script>"
    h = pf.render_html(pf.roll_up({evil: [_xss_filing(sym=evil)]}))
    assert "<script" not in h.lower() and "&lt;" in h        # escaped (symbol is upper-cased)


# ── P2 ───────────────────────────────────────────────────────────────────────

def test_build_analysis_artifacts_propagates_real_errors(monkeypatch):
    import qscreen_analyze
    from types import SimpleNamespace

    def boom(*a, **k):
        raise ValueError("boom")
    monkeypatch.setattr(qscreen_analyze, "analyze", boom)
    with pytest.raises(ValueError):
        e.build_analysis_artifacts({"metadata": {"symbol": "X"}}, SimpleNamespace(symbol="X", _profile=None))


def test_year_from_label_is_strict():
    assert s._year_from_label("for the year ended 31 December 2023") == 2023
    assert s._year_from_label("2022-2023") == 2023          # last token of a range
    assert s._year_from_label("note 2008-2025") == 2025
    assert s._year_from_label("IFRS 9") is None
    assert s._year_from_label("ref 202312345 end") is None    # year embedded in a longer number


# ── P3 ───────────────────────────────────────────────────────────────────────

def test_dividend_payout_none_in_loss_year():
    r = az.compute_ratios({"years": {"2023": {"metrics": {"CF_DIVIDENDS_PAID": 50, "IS_NET_INCOME": -100,
                                                          "IS_REVENUE": 1, "BS_TOTAL_EQUITY": 1}}}},
                          "industrial")["2023"]
    assert r["dividend_payout"]["value"] is None


def test_compare_ties_share_a_rank():
    groups = {"AAA": [_bankf("AAA", IS_NET_INCOME=10, BS_TOTAL_EQUITY=100)],
              "BBB": [_bankf("BBB", IS_NET_INCOME=10, BS_TOTAL_EQUITY=100)]}   # identical ROE
    profs = {"AAA": {"archetype": "conventional_bank"}, "BBB": {"archetype": "conventional_bank"}}
    out = az.compare("AAA", groups, profs)
    assert sorted(r["ranks"]["roe"] for r in out["rows"]) == [1, 1]
