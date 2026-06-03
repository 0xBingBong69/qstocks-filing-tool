"""Route tests for the local browser app (no network, no API key)."""
from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

import qscreen_app as app_mod


@pytest.fixture
def client():
    app_mod.app.config["TESTING"] = True
    return app_mod.app.test_client()


def test_index_renders_without_leftover_placeholders(client, monkeypatch):
    monkeypatch.delenv("INGEST_TOKEN", raising=False)
    html = client.get("/").get_data(as_text=True)
    assert re.search(r"__[A-Z_]+__", html) is None
    assert "const UPLOAD_ENABLED = false;" in html
    assert 'name="provider"' in html and 'name="subsector"' in html


def test_index_upload_flag_follows_token(client, monkeypatch):
    monkeypatch.setenv("INGEST_TOKEN", "tok")
    assert "const UPLOAD_ENABLED = true;" in client.get("/").get_data(as_text=True)


def test_index_lists_all_providers(client):
    html = client.get("/").get_data(as_text=True)
    for v in ("minimax", "openrouter", "kimi", "openai", "anthropic"):
        assert f'value="{v}"' in html
    assert "PROVIDER_INFO = {" in html
    # the clickable key-signup links must be embedded for non-technical users
    assert "platform.moonshot.ai/console/api-keys" in html   # global endpoint, not .cn
    assert "console.anthropic.com/settings/keys" in html
    assert "openrouter.ai/keys" in html
    assert "platform.openai.com/api-keys" in html
    assert "platform.minimax.io" in html


def test_extract_requires_pdf(client):
    r = client.post("/extract", data={"symbol": "QIBK", "subsector": "Islamic Bank", "year": "2024"})
    assert r.status_code == 400
    assert "no PDF" in r.get_json()["error"]


def test_upload_refused_without_token(client, monkeypatch):
    monkeypatch.delenv("INGEST_TOKEN", raising=False)
    r = client.post("/upload", json={"filing": app_mod.engine.empty_filing()})
    assert r.status_code == 400
    assert "INGEST_TOKEN" in r.get_json()["error"]


def test_upload_rejects_nonconforming(client, monkeypatch):
    monkeypatch.setenv("INGEST_TOKEN", "tok")
    r = client.post("/upload", json={"filing": app_mod.engine.empty_filing()})  # empty statements
    assert r.status_code == 400
    assert "problems" in r.get_json()


def test_upload_posts_clean_filing(client, monkeypatch):
    monkeypatch.setenv("INGEST_TOKEN", "tok")
    captured = {}

    def fake_upload(filing, args):
        captured.update(url=args.api_url, token=args.token)
        return {"id": "filing_1"}

    monkeypatch.setattr(app_mod.engine, "upload_filing", fake_upload)

    good = app_mod.engine.empty_filing()
    good["metadata"].update({"symbol": "QNBK", "sector": "conventional_bank",
                             "fiscal_year": 2023, "fiscal_period": "FY"})
    good["audit"].update({"opinion_type": "unqualified", "verbatim_text": "In our opinion …"})
    good["statements"].append({"type": "income_statement", "title": "IS", "period_label": "2023",
                               "verbatim_text": "NII 1",
                               "line_items": [{"account_code": "IS_NET_INTEREST", "label_verbatim": "NII",
                                               "value": 1}]})
    good["notes"].append({"number": "1", "title": "x", "category": "other",
                          "structured": {}, "verbatim_text": "…"})
    r = client.post("/upload", json={"filing": good})
    assert r.status_code == 200 and r.get_json()["ok"] is True
    assert captured["token"] == "tok"


def test_segments_route_analyzes_filing(client):
    filing = {"metadata": {"symbol": "QNBK", "fiscal_year": 2023, "currency": "QAR"},
              "segments": [{"dimension": "geography", "name": "Turkey", "metrics": {"revenue": 50},
                            "comparatives": [{"period_label": "2022", "metrics": {"revenue": 70}}]}]}
    r = client.post("/segments", json={"filing": filing})
    assert r.status_code == 200
    turkey = r.get_json()["dimensions"]["geography"]["segments"][0]
    assert turkey["name"] == "Turkey" and turkey["fx_exposed"] and turkey["currency"] == "TRY"


def test_segments_route_missing_filing(client):
    assert client.post("/segments", json={}).status_code == 400


def test_analyze_route(client):
    li = [{"account_code": c, "label_verbatim": c, "value": v,
           "comparatives": [{"period_label": "2022", "value": pv}]}
          for c, v, pv in [("IS_NET_INCOME", 15000, 13000), ("BS_TOTAL_EQUITY", 100000, 95000)]]
    filing = {"metadata": {"symbol": "QNBK", "fiscal_year": 2023, "fiscal_period": "FY", "currency": "QAR"},
              "statements": [{"type": "balance_sheet", "verbatim_text": "x", "line_items": li}]}
    r = client.post("/analyze", json={"filing": filing})
    assert r.status_code == 200
    j = r.get_json()
    assert j["archetype"] == "conventional_bank" and "ratios" in j and "red_flags" in j


def test_analyze_route_missing(client):
    assert client.post("/analyze", json={}).status_code == 400


def test_dcf_route(client):
    li = [{"account_code": c, "label_verbatim": c, "value": v,
           "comparatives": [{"period_label": "2022", "value": pv}]}
          for c, v, pv in [("IS_NET_INCOME", 15000, 13000), ("BS_TOTAL_EQUITY", 100000, 95000)]]
    filing = {"metadata": {"symbol": "QNBK", "fiscal_year": 2023, "fiscal_period": "FY", "currency": "QAR"},
              "statements": [{"type": "balance_sheet", "verbatim_text": "x", "line_items": li}]}
    r = client.post("/dcf", json={"filing": filing, "assumptions": {"growth": 0.05}, "shares": 1000})
    assert r.status_code == 200
    j = r.get_json()
    assert j["valuation"]["model"] == "residual_income" and j["valuation"]["per_share"] is not None


def test_dcf_route_missing(client):
    assert client.post("/dcf", json={}).status_code == 400


def _bank_filing(sym, ni, eq):
    li = [{"account_code": c, "label_verbatim": c, "value": v,
           "comparatives": [{"period_label": "2022", "value": v}]}
          for c, v in [("IS_NET_INCOME", ni), ("BS_TOTAL_EQUITY", eq)]]
    return {"metadata": {"symbol": sym, "fiscal_year": 2023, "fiscal_period": "FY", "currency": "QAR"},
            "statements": [{"type": "income_statement", "verbatim_text": "x", "line_items": li}]}


def test_compare_route(client):
    r = client.post("/compare", json={"filings": [_bank_filing("QNBK", 15000, 100000),
                                                  _bank_filing("CBQK", 2500, 30000)]})
    assert r.status_code == 200
    j = r.get_json()
    assert j["target"] == "QNBK" and j["rows"][0]["symbol"] == "QNBK"
    assert j["rows"][0]["ranks"]["roe"] == 1


def test_compare_route_missing(client):
    assert client.post("/compare", json={}).status_code == 400


def test_upload_route_folds_analysis(client, monkeypatch):
    monkeypatch.setenv("INGEST_TOKEN", "tok")
    captured = {}
    monkeypatch.setattr(app_mod.engine, "upload_filing",
                        lambda filing, args, analysis=None: captured.update(analysis=analysis) or {"ok": 1})
    f = app_mod.engine.empty_filing()
    f["metadata"].update({"symbol": "QNBK", "sector": "conventional_bank",
                          "fiscal_year": 2023, "fiscal_period": "FY"})
    f["audit"].update({"opinion_type": "unqualified", "verbatim_text": "In our opinion …"})
    f["statements"].append({"type": "income_statement", "title": "IS", "period_label": "2023",
                            "verbatim_text": "NII 1",
                            "line_items": [{"account_code": "IS_NET_INTEREST", "label_verbatim": "NII",
                                            "value": 1}]})
    f["notes"].append({"number": "1", "title": "x", "category": "other",
                       "structured": {}, "verbatim_text": "…"})
    r = client.post("/upload", json={"filing": f, "analysis": {"x": 1}, "with_analysis": True})
    assert r.status_code == 200 and captured["analysis"] == {"x": 1}


def test_subsector_taxonomy_maps_to_valid_categories():
    # Every sub-sector the UI offers must map to one of the engine's 5 sectors.
    for sub, cat in app_mod.SUBSECTOR_TO_EXTRACTION.items():
        assert cat in app_mod.engine.SECTORS, f"{sub} → {cat} not a valid sector"


def test_known_symbols_have_known_subsectors():
    for sym, sub in app_mod.SYMBOL_SUBSECTOR.items():
        assert sub in app_mod.SUBSECTOR_TO_EXTRACTION, f"{sym} → {sub} not in taxonomy"
