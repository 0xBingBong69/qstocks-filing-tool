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
