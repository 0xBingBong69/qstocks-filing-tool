"""Tests for the Qatar (QSE) per-stock temporal knowledge base."""
from __future__ import annotations

import json
from pathlib import Path

import qatar


ARCHETYPES = {"conventional_bank", "islamic_bank", "insurance", "industrial", "other"}


def test_all_55_tickers_build_valid_profiles():
    tickers = qatar.all_tickers()
    assert len(tickers) == 55
    for t in tickers:
        p = qatar.load_profile(t)
        assert p["archetype"] in ARCHETYPES, t
        assert p["company_name"] and p["watch_kpis"], t
        assert p["names"] and p["framework_timeline"], t


def test_load_profile_normalizes_and_handles_unknown():
    assert qatar.load_profile("qnbk")["ticker"] == "QNBK"
    assert qatar.load_profile("QNBK.QA")["ticker"] == "QNBK"
    assert qatar.load_profile("ZZZZ") is None


def test_qnb_acquisition_timeline():
    assert "EGP" not in qatar.profile_for_year("QNBK", 2012)["active_currencies"]
    assert "EGP" in qatar.profile_for_year("QNBK", 2013)["active_currencies"]   # NSGB 2013
    assert "TRY" not in qatar.profile_for_year("QNBK", 2015)["active_currencies"]
    assert "TRY" in qatar.profile_for_year("QNBK", 2016)["active_currencies"]   # Finansbank 2016


def test_name_changes_resolve_by_year():
    assert qatar.profile_for_year("DUBK", 2018)["name_as_of"] == "Barwa Bank"
    assert qatar.profile_for_year("DUBK", 2023)["name_as_of"] == "Dukhan Bank"
    assert qatar.profile_for_year("QFBQ", 2020)["name_as_of"] == "Qatar First Bank"
    assert qatar.profile_for_year("QFBQ", 2023)["name_as_of"] == "Lesha Bank"


def test_merger_events_in_force():
    assert any("Masraf" in e["title"] for e in qatar.profile_for_year("KCBK", 2021)["active_events"])
    assert any("al khaliji" in e["title"] for e in qatar.profile_for_year("MARK", 2022)["active_events"])


def test_banks_inherit_regulatory_timeline():
    qnb = qatar.load_profile("QNBK")
    assert any("Basel III" in e["title"] for e in qnb["events"])
    assert any("IFRS 9" in e["title"] for e in qnb["events"])
    # IFRS 9 only in force from 2018
    e2017 = qatar.profile_for_year("QNBK", 2017)["active_events"]
    e2019 = qatar.profile_for_year("QNBK", 2019)["active_events"]
    assert not any("IFRS 9" in e["title"] for e in e2017)
    assert any("IFRS 9" in e["title"] for e in e2019)


def test_islamic_bank_framework_and_no_interest_kpis():
    qib = qatar.load_profile("QIBK")
    assert qib["archetype"] == "islamic_bank"
    assert "Islamic" in qib["framework_timeline"][0]["framework"]
    assert "KPI_NIM" not in qib["watch_kpis"]          # no interest margin for Islamic banks


def test_profile_for_year_none_year_is_static_view():
    p = qatar.profile_for_year("ORDS", None)
    assert p["as_of_year"] is None and p["name_as_of"] == p["company_name"]


def test_exported_json_matches_built_profiles():
    """The committed qatar/profiles/*.json must not drift from the seed."""
    pdir = Path(qatar.__file__).resolve().parent / "profiles"
    assert pdir.is_dir(), "run: python3 -c 'import qatar; qatar.export_json()'"
    on_disk = sorted(p.stem for p in pdir.glob("*.json"))
    assert on_disk == qatar.all_tickers()
    for t in qatar.all_tickers():
        disk = json.loads((pdir / f"{t}.json").read_text(encoding="utf-8"))
        assert disk == qatar.build_profile(t), f"{t}.json drifted — regenerate with export_json()"


def test_backcompat_maps_for_app():
    assert len(qatar.SYMBOL_SUBSECTOR) == 55
    assert set(qatar.SUBSECTOR_TO_EXTRACTION.values()) <= ARCHETYPES
    for sym, sub in qatar.SYMBOL_SUBSECTOR.items():
        assert sub in qatar.SUBSECTOR_TO_EXTRACTION
