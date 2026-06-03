"""Tests for the segment analyzer (Phase 2)."""
from __future__ import annotations

import qatar
import qscreen_analyze as an


def _qnb_geo_filing():
    return {"metadata": {"symbol": "QNBK", "fiscal_year": 2023, "fiscal_period": "FY",
                         "currency": "QAR"},
            "segments": [
                {"dimension": "geography", "name": "Qatar", "metrics": {"revenue": 100, "net_profit": 40},
                 "comparatives": [{"period_label": "2022", "metrics": {"revenue": 90, "net_profit": 38}}]},
                {"dimension": "geography", "name": "Turkey", "metrics": {"revenue": 50, "net_profit": 5},
                 "comparatives": [{"period_label": "2022", "metrics": {"revenue": 70, "net_profit": 9}}]},
                {"dimension": "geography", "name": "Egypt", "currency": "EGP",
                 "metrics": {"revenue": 30, "net_profit": 8},
                 "comparatives": [{"period_label": "2022", "metrics": {"revenue": 28, "net_profit": 7}}]}]}


def test_num_and_yoy_helpers():
    assert an._num("1,234") == 1234.0
    assert an._num("(50)") == -50.0
    assert an._num("n/a") is None
    assert an._yoy(110, 100) == 0.1
    assert an._yoy(5, 0) is None


def test_totals_yoy_and_share():
    out = an.analyze_segments(_qnb_geo_filing())
    geo = out["dimensions"]["geography"]
    assert geo["total"]["revenue"] == 180.0
    turkey = next(r for r in geo["segments"] if r["name"] == "Turkey")
    assert round(turkey["yoy"]["revenue"], 4) == round((50 - 70) / 70, 4)
    assert round(turkey["share"]["revenue"], 4) == round(50 / 180, 4)


def test_fx_flag_from_explicit_currency():
    out = an.analyze_segments(_qnb_geo_filing())          # no profile
    egypt = next(r for r in out["dimensions"]["geography"]["segments"] if r["name"] == "Egypt")
    assert egypt["fx_exposed"] and egypt["currency"] == "EGP" and egypt["fx_note"]


def test_fx_and_events_inferred_from_profile():
    out = an.analyze_segments(_qnb_geo_filing(), qatar.profile_for_year("QNBK", 2023))
    turkey = next(r for r in out["dimensions"]["geography"]["segments"] if r["name"] == "Turkey")
    assert turkey["fx_exposed"] and turkey["currency"] == "TRY"      # inferred via profile subsidiary
    assert any("Finansbank" in ev for ev in turkey["events"])
    qatar_seg = next(r for r in out["dimensions"]["geography"]["segments"] if r["name"] == "Qatar")
    assert not qatar_seg["fx_exposed"]                              # home market, no FX


def test_segments_sorted_by_revenue_desc():
    out = an.analyze_segments(_qnb_geo_filing())
    names = [r["name"] for r in out["dimensions"]["geography"]["segments"]]
    assert names == ["Qatar", "Turkey", "Egypt"]


def test_no_segments_warns():
    out = an.analyze_segments({"metadata": {"symbol": "QNBK"}, "segments": []})
    assert out["dimensions"] == {} and out["warnings"]


# ── ratios / trends / red flags (Phase 3) ────────────────────────────────────

def _bank_series():
    return {"symbol": "QNBK", "currency": "QAR", "restatements": [], "years": {
        "2022": {"metrics": {"BS_TOTAL_EQUITY": 95000, "BS_TOTAL_ASSETS": 1150000, "IS_NET_INCOME": 13000}},
        "2023": {"metrics": {"IS_NET_INCOME": 15000, "BS_TOTAL_EQUITY": 100000, "BS_TOTAL_ASSETS": 1200000,
                             "IS_NET_INTEREST": 24000, "BS_LOANS": 800000, "BS_CUSTOMER_DEPOSITS": 850000,
                             "KPI_CAR": 19.5, "KPI_NPL": 3.0, "KPI_COST_INCOME": 22.0}}}}


def test_bank_ratios_golden():
    r = an.compute_ratios(_bank_series(), "conventional_bank")["2023"]
    assert round(r["roe"]["value"], 4) == round(15000 / 97500, 4) and r["roe"]["basis"] == "computed"
    assert round(r["roa"]["value"], 6) == round(15000 / 1175000, 6)
    assert round(r["nim"]["value"], 6) == round(24000 / 1175000, 6) and r["nim"]["basis"] == "computed"
    assert r["cost_income"] == {"value": 22.0, "basis": "reported"}      # printed KPI preferred
    assert round(r["ldr"]["value"], 4) == round(800000 / 850000, 4)
    assert r["npl"]["value"] == 3.0 and r["car"]["value"] == 19.5


def test_islamic_bank_has_no_nim():
    r = an.compute_ratios(_bank_series(), "islamic_bank")["2023"]
    assert "nim" not in r and "roe" in r


def test_insurance_combined_ratio_computed_from_parts():
    series = {"years": {"2023": {"metrics": {"IS_CLAIMS": 60, "IS_NET_PREMIUMS": 100,
                                             "KPI_EXPENSE_RATIO": 25.0, "BS_TOTAL_EQUITY": 200,
                                             "IS_NET_INCOME": 20}}}}
    r = an.compute_ratios(series, "insurance")["2023"]
    assert round(r["loss_ratio"]["value"], 3) == 0.6 and r["loss_ratio"]["basis"] == "computed"
    assert round(r["combined_ratio"]["value"], 3) == round(0.6 + 0.25, 3)   # 60% loss + 25% expense


def test_industrial_ratios_and_fcf():
    series = {"years": {"2023": {"metrics": {"IS_REVENUE": 1000, "IS_NET_INCOME": 120,
                                             "IS_OPERATING_PROFIT": 200, "BS_TOTAL_EQUITY": 800,
                                             "CF_OCF": 300, "CF_CAPEX": -120}}}}
    r = an.compute_ratios(series, "industrial")["2023"]
    assert r["net_margin"]["value"] == 0.12 and r["operating_margin"]["value"] == 0.2
    assert r["fcf"]["value"] == 180 and r["fcf"]["basis"] == "computed"     # OCF + capex


def test_compute_trends_yoy_and_cagr():
    series = {"years": {"2021": {"metrics": {"IS_NET_INCOME": 100}},
                        "2023": {"metrics": {"IS_NET_INCOME": 144}}}}
    t = an.compute_trends(series, "conventional_bank")["IS_NET_INCOME"]
    assert t["span_years"] == 2 and round(t["cagr"], 3) == 0.2          # 100→144 over 2y = 20%


def test_red_flags_healthy_vs_stressed():
    assert an.red_flags(_bank_series(), an.compute_ratios(_bank_series(), "conventional_bank")) == []
    stressed = {"restatements": [{"x": 1}], "years": {
        "2022": {"metrics": {"BS_TOTAL_EQUITY": 100}},
        "2023": {"metrics": {"KPI_CAR": 12.0, "KPI_NPL": 5.5, "BS_TOTAL_EQUITY": 90}}}}
    rules = {f["rule"] for f in an.red_flags(stressed, an.compute_ratios(stressed, "conventional_bank"))}
    assert {"low_car", "high_npl", "equity_decline", "restatement"} <= rules


def test_red_flags_audit_and_going_concern_from_filings():
    series = {"years": {}}
    filings = [{"metadata": {"fiscal_year": 2023},
                "audit": {"opinion_type": "qualified",
                          "material_uncertainty_going_concern": {"present": True}}}]
    rules = {f["rule"] for f in an.red_flags(series, {}, None, filings)}
    assert {"going_concern", "audit_opinion"} <= rules


def _filing(year, items):
    li = [{"account_code": c, "label_verbatim": c, "value": v,
           "comparatives": [{"period_label": str(year - 1), "value": pv}]} for c, v, pv in items]
    return {"metadata": {"symbol": "QNBK", "fiscal_year": year, "fiscal_period": "FY", "currency": "QAR"},
            "statements": [{"type": "balance_sheet", "verbatim_text": "x", "line_items": li}]}


def test_analyze_orchestrator_end_to_end():
    import qatar
    f = _filing(2023, [("IS_NET_INCOME", 15000, 13000), ("BS_TOTAL_EQUITY", 100000, 95000),
                       ("BS_TOTAL_ASSETS", 1200000, 1150000)])
    out = an.analyze("QNBK", [f], qatar.profile_for_year("QNBK", 2023))
    assert out["archetype"] == "conventional_bank"
    assert set(out["years"]) == {"2022", "2023"}                      # comparative gives the prior year
    assert out["ratios"]["2023"]["roe"]["value"] is not None
    assert "trends" in out and "red_flags" in out and "segments" in out


def test_analyst_narrative_uses_call_llm(monkeypatch):
    import qscreen_ingest
    monkeypatch.setattr(qscreen_ingest, "call_llm", lambda msgs, a: "QNB delivered steady growth ...")
    out = an.analyze("QNBK", [_filing(2023, [("IS_NET_INCOME", 1, 1)])], None,
                     narrative=True, args=None)
    assert out["narrative"].startswith("QNB delivered")


def test_analyst_narrative_error_is_captured(monkeypatch):
    import qscreen_ingest

    def boom(msgs, a):
        raise SystemExit("no API key")
    monkeypatch.setattr(qscreen_ingest, "call_llm", boom)
    out = an.analyze("QNBK", [_filing(2023, [("IS_NET_INCOME", 1, 1)])], None, narrative=True)
    assert "no API key" in out["narrative_error"]


# ── peer comparison ──────────────────────────────────────────────────────────

def _bank(sym, ni, eq, ci=None):
    items = [("IS_NET_INCOME", ni, ni), ("BS_TOTAL_EQUITY", eq, eq)]
    if ci is not None:
        items.append(("KPI_COST_INCOME", ci, ci))
    li = [{"account_code": c, "label_verbatim": c, "value": v,
           "comparatives": [{"period_label": "2022", "value": pv}]} for c, v, pv in items]
    return {"metadata": {"symbol": sym, "fiscal_year": 2023, "fiscal_period": "FY", "currency": "QAR"},
            "statements": [{"type": "income_statement", "verbatim_text": "x", "line_items": li}]}


def test_group_by_symbol():
    g = an.group_by_symbol([_bank("QNBK", 1, 1), _bank("QNBK", 2, 2), _bank("CBQK", 1, 1)])
    assert set(g) == {"QNBK", "CBQK"} and len(g["QNBK"]) == 2


def test_compare_ranks_and_puts_target_first():
    import qatar
    groups = {"QNBK": [_bank("QNBK", 15000, 100000, 22.0)],   # ROE 15%, best cost/income
              "CBQK": [_bank("CBQK", 2500, 30000, 30.0)],
              "DHBK": [_bank("DHBK", 1200, 20000, 40.0)]}      # worst on both
    profs = {s: qatar.profile_for_year(s, 2023) for s in groups}
    out = an.compare("QNBK", groups, profs)
    assert out["archetype"] == "conventional_bank"
    assert out["rows"][0]["symbol"] == "QNBK" and out["rows"][0]["is_target"]
    qnb = out["rows"][0]
    assert qnb["ranks"]["roe"] == 1                 # highest ROE
    assert qnb["ranks"]["cost_income"] == 1         # lowest cost/income wins (low-is-better)
    dhb = next(r for r in out["rows"] if r["symbol"] == "DHBK")
    assert dhb["ranks"]["roe"] == 3
