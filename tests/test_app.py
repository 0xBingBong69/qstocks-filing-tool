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


def test_index_lists_local_providers_and_modes(client):
    html = client.get("/").get_data(as_text=True)
    for v in ("ollama", "lmstudio", "llamacpp", "jan", "gpt4all", "mlx"):
        assert f'value="{v}"' in html
    assert 'name="mode"' in html and 'name="no_llm"' in html  # Basic/Pro + offline controls
    assert '"local": true' in html                            # PROVIDER_INFO carries the flag
    assert "no API key" in html or "no key" in html


def _fake_filing():
    f = app_mod.engine.empty_filing()
    f["statements"].append({"type": "income_statement", "title": "IS", "period_label": "2024",
                            "verbatim_text": "x",
                            "line_items": [{"account_code": None, "label_verbatim": "Rev", "value": 1}]})
    return f


def test_extract_form_threads_basic_mode(client, monkeypatch):
    # The /extract route must map mode=basic → guided and shrink the window size.
    import io
    captured = {}

    def fake_extract_filing(pages, args):
        captured["guided"] = args.guided
        captured["pages_per_chunk"] = args.pages_per_chunk
        return _fake_filing()

    monkeypatch.setattr(app_mod.engine, "resolve_provider",
                        lambda args: {"name": "openai", "model": "gpt-4o", "local": False,
                                      "base_url": "https://api.openai.com/v1", "kind": "openai", "key": "k"})
    monkeypatch.setattr(app_mod.engine, "pdf_to_pages", lambda path: ([{"num": 1, "text": "x"}], "sha"))
    monkeypatch.setattr(app_mod.engine, "extract_filing", fake_extract_filing)

    r = client.post("/extract", data={"symbol": "QNBK", "subsector": "Commercial Bank",
                                       "year": "2024", "period": "FY", "provider": "openai",
                                       "mode": "basic",
                                       "pdf": (io.BytesIO(b"%PDF-1.4 fake"), "x.pdf")},
                    content_type="multipart/form-data")
    assert r.status_code == 200, r.get_json()
    assert captured["guided"] is True                         # basic forces guided even for cloud
    assert captured["pages_per_chunk"] == app_mod.engine.GUIDED_DEFAULT_PAGES


def test_extract_no_llm_needs_no_provider(client, monkeypatch):
    # Fully-offline (--no-llm) works even when no provider/key can be resolved.
    import io
    captured = {}

    def boom(args):
        raise SystemExit("No LLM provider selected and no provider API key found.")

    def fake_extract_filing(pages, args):
        captured["guided"] = args.guided
        captured["no_llm"] = args.no_llm
        return _fake_filing()

    monkeypatch.setattr(app_mod.engine, "resolve_provider", boom)
    monkeypatch.setattr(app_mod.engine, "pdf_to_pages", lambda path: ([{"num": 1, "text": "x"}], "sha"))
    monkeypatch.setattr(app_mod.engine, "extract_filing", fake_extract_filing)

    r = client.post("/extract", data={"symbol": "QNBK", "subsector": "Commercial Bank",
                                       "year": "2024", "period": "FY", "no_llm": "1",
                                       "pdf": (io.BytesIO(b"%PDF-1.4 fake"), "x.pdf")},
                    content_type="multipart/form-data")
    assert r.status_code == 200, r.get_json()
    assert captured["no_llm"] is True and captured["guided"] is True


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


def _statement_filing():
    li = [{"account_code": c, "label_verbatim": c, "value": v,
           "comparatives": [{"period_label": "2022", "value": v}]}
          for c, v in [("IS_NET_INCOME", 15000), ("BS_TOTAL_EQUITY", 100000)]]
    return {"metadata": {"symbol": "QNBK", "fiscal_year": 2023, "fiscal_period": "FY", "currency": "QAR"},
            "statements": [{"type": "income_statement", "title": "IS", "period_label": "2023",
                            "verbatim_text": "x", "line_items": li}]}


def test_workbook_route_returns_xlsx(client):
    r = client.post("/workbook", json={"filing": _statement_filing()})
    assert r.status_code == 200
    assert "spreadsheetml" in r.headers["Content-Type"]
    assert r.data[:2] == b"PK" and len(r.data) > 2000              # a real .xlsx (zip)


def test_workbook_route_missing(client):
    assert client.post("/workbook", json={}).status_code == 400


def test_export_csv_route(client):
    r = client.post("/export.csv", json={"filing": _statement_filing()})
    assert r.status_code == 200 and r.headers["Content-Type"].startswith("text/csv")
    assert r.get_data(as_text=True).splitlines()[0].split(",")[0] == "statement_type"


def test_export_csv_route_missing(client):
    assert client.post("/export.csv", json={}).status_code == 400


def test_workbook_route_multi_filing(client):
    def yf(y, ni):
        return {"metadata": {"symbol": "QNBK", "fiscal_year": y, "fiscal_period": "FY", "currency": "QAR"},
                "statements": [{"type": "income_statement", "title": "IS", "period_label": str(y),
                                "verbatim_text": "x", "line_items": [
                                    {"account_code": "IS_NET_INCOME", "label_verbatim": "Profit", "value": ni,
                                     "comparatives": [{"period_label": str(y - 1), "value": ni - 1000}]}]}]}
    r = client.post("/workbook", json={"filings": [yf(2022, 14000), yf(2023, 15000)]})
    assert r.status_code == 200 and r.data[:2] == b"PK"


def test_index_has_workbook_button(client):
    h = client.get("/").get_data(as_text=True)
    assert "runWorkbook" in h and "Excel workbook" in h


def test_statements_route(client):
    r = client.post("/statements", json={"filing": _statement_filing()})
    assert r.status_code == 200
    assert r.get_json()["html"].startswith("<!doctype html>")


def test_statements_route_missing(client):
    assert client.post("/statements", json={}).status_code == 400


def test_ttm_route(client):
    def f(y, p, ni):
        return {"metadata": {"symbol": "QNBK", "fiscal_year": y, "fiscal_period": p},
                "statements": [{"type": "income_statement",
                                "line_items": [{"account_code": "IS_NET_INCOME", "value": ni}]}]}
    r = client.post("/ttm", json={"filings": [f(2023, "9M", 10500), f(2023, "FY", 14000),
                                              f(2024, "9M", 12000)]})
    assert r.status_code == 200
    assert r.get_json()["flows"]["IS_NET_INCOME"] == 15500


def test_ttm_route_missing(client):
    assert client.post("/ttm", json={}).status_code == 400


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


def test_report_route(client):
    li = [{"account_code": c, "label_verbatim": c, "value": v,
           "comparatives": [{"period_label": "2022", "value": v}]}
          for c, v in [("IS_NET_INCOME", 15000), ("BS_TOTAL_EQUITY", 100000)]]
    filing = {"metadata": {"symbol": "QNBK", "fiscal_year": 2023, "fiscal_period": "FY", "currency": "QAR"},
              "statements": [{"type": "income_statement", "verbatim_text": "x", "line_items": li}]}
    r = client.post("/report", json={"filing": filing})
    assert r.status_code == 200
    j = r.get_json()
    assert j["symbol"] == "QNBK" and j["html"].startswith("<!doctype html>") and j["markdown"]


def test_report_route_missing(client):
    assert client.post("/report", json={}).status_code == 400


def test_portfolio_route(client):
    def f(sym, ni, eq):
        li = [{"account_code": c, "label_verbatim": c, "value": v,
               "comparatives": [{"period_label": "2022", "value": v}]}
              for c, v in [("IS_NET_INCOME", ni), ("BS_TOTAL_EQUITY", eq)]]
        return {"metadata": {"symbol": sym, "fiscal_year": 2023, "fiscal_period": "FY", "currency": "QAR"},
                "statements": [{"type": "income_statement", "verbatim_text": "x", "line_items": li}]}
    r = client.post("/portfolio", json={"filings": [f("QNBK", 15000, 100000), f("QIBK", 4000, 40000)]})
    assert r.status_code == 200
    j = r.get_json()
    assert j["count"] == 2 and j["html"].startswith("<!doctype html>")
    assert {row["symbol"] for row in j["rows"]} == {"QNBK", "QIBK"}


def test_portfolio_route_missing(client):
    assert client.post("/portfolio", json={}).status_code == 400


def test_extract_non_numeric_year_is_400(client):
    import io
    r = client.post("/extract", data={"symbol": "QNBK", "subsector": "Commercial Bank",
                                       "year": "20xx", "period": "FY",
                                       "pdf": (io.BytesIO(b"%PDF-1.4 fake"), "x.pdf")},
                    content_type="multipart/form-data")
    assert r.status_code == 400 and "year" in r.get_json()["error"]


def test_dcf_route_bad_assumptions_is_400(client):
    li = [{"account_code": "IS_NET_INCOME", "label_verbatim": "x", "value": 1,
           "comparatives": [{"period_label": "2022", "value": 1}]},
          {"account_code": "BS_TOTAL_EQUITY", "label_verbatim": "x", "value": 1,
           "comparatives": [{"period_label": "2022", "value": 1}]}]
    filing = {"metadata": {"symbol": "QNBK", "fiscal_year": 2023, "fiscal_period": "FY", "currency": "QAR"},
              "statements": [{"type": "income_statement", "verbatim_text": "x", "line_items": li}]}
    # terminal_growth >= discount_rate makes the Gordon model raise — must be a clean 400, not 500.
    r = client.post("/dcf", json={"filing": filing,
                                  "assumptions": {"discount_rate": 0.02, "terminal_growth": 0.05}})
    assert r.status_code == 400


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


# ── automatic fiscal-year / period detection (no fields to fill) ─────────────

def _pg(text):
    return [{"num": 1, "text": text}]


@pytest.mark.parametrize("text,year,period", [
    ("Qatar Islamic Bank\nConsolidated financial statements\nFor the year ended 31 December 2024", 2024, "FY"),
    ("Interim condensed statements\nFor the six months ended 30 June 2023", 2023, "H1"),
    ("Review report — for the nine-month period ended 30 September 2022", 2022, "9M"),
    ("Interim financial information\nThree months ended 31 March 2021", 2021, "Q1"),
    ("ANNUAL REPORT 2019", 2019, "FY"),
])
def test_detect_fiscal_year_period(text, year, period):
    det = app_mod.engine.detect_fiscal_year_period(_pg(text))
    assert det["fiscal_year"] == year and det["fiscal_period"] == period


def test_detect_fiscal_year_period_unknown_is_none():
    det = app_mod.engine.detect_fiscal_year_period(_pg("nothing dateable here"))
    assert det["fiscal_year"] is None and det["fiscal_period"] is None


def _stub_openai(monkeypatch):
    monkeypatch.setattr(app_mod.engine, "resolve_provider",
                        lambda args: {"name": "openai", "model": "gpt-4o", "local": False,
                                      "base_url": "https://api.openai.com/v1", "kind": "openai", "key": "k"})


def test_extract_detects_year_when_omitted(client, monkeypatch):
    import io
    captured = {}
    _stub_openai(monkeypatch)
    monkeypatch.setattr(app_mod.engine, "pdf_to_pages",
                        lambda path: (_pg("For the year ended 31 December 2024"), "sha"))
    monkeypatch.setattr(app_mod.engine, "extract_filing",
                        lambda pages, args: captured.update(year=args.year, period=args.period) or _fake_filing())

    r = client.post("/extract", data={"symbol": "QNBK", "subsector": "Commercial Bank", "provider": "openai",
                                       "pdf": (io.BytesIO(b"%PDF-1.4 fake"), "x.pdf")},
                    content_type="multipart/form-data")
    assert r.status_code == 200, r.get_json()
    assert captured["year"] == 2024 and captured["period"] == "FY"          # read off the PDF
    assert r.get_json()["filing"]["metadata"]["fiscal_year"] == 2024


def test_extract_falls_back_when_year_undetectable(client, monkeypatch):
    import io
    _stub_openai(monkeypatch)
    monkeypatch.setattr(app_mod.engine, "pdf_to_pages", lambda path: (_pg("no dates here"), "sha"))
    monkeypatch.setattr(app_mod.engine, "extract_filing",
                        lambda *a: pytest.fail("extract_filing must not run without a year"))

    r = client.post("/extract", data={"symbol": "QNBK", "subsector": "Commercial Bank", "provider": "openai",
                                       "pdf": (io.BytesIO(b"%PDF-1.4 fake"), "x.pdf")},
                    content_type="multipart/form-data")
    assert r.status_code == 422 and r.get_json()["need_year"] is True


def test_extract_manual_year_overrides_detection(client, monkeypatch):
    import io
    captured = {}
    _stub_openai(monkeypatch)
    # PDF reads 2024, but the user typed 2021/9M in the fallback box → the user wins.
    monkeypatch.setattr(app_mod.engine, "pdf_to_pages",
                        lambda path: (_pg("For the year ended 31 December 2024"), "sha"))
    monkeypatch.setattr(app_mod.engine, "extract_filing",
                        lambda pages, args: captured.update(year=args.year, period=args.period) or _fake_filing())

    r = client.post("/extract", data={"symbol": "QNBK", "subsector": "Commercial Bank", "provider": "openai",
                                       "year": "2021", "period": "9M",
                                       "pdf": (io.BytesIO(b"%PDF-1.4 fake"), "x.pdf")},
                    content_type="multipart/form-data")
    assert r.status_code == 200, r.get_json()
    assert captured["year"] == 2021 and captured["period"] == "9M"


# ── Settings: save an API key to .env, no terminal/restart ───────────────────

def test_settings_saves_key_and_masks(client, monkeypatch):
    calls = []
    monkeypatch.setattr(app_mod.engine, "set_dotenv_value",
                        lambda k, v, path=None: calls.append((k, v)))
    r = client.post("/settings", json={"provider": "claude", "key": "sk-ant-wxyz9876"})  # alias → anthropic
    assert r.status_code == 200
    j = r.get_json()
    assert j["ok"] and j["provider"] == "anthropic" and j["masked_key"] == "••••9876"
    assert ("ANTHROPIC_API_KEY", "sk-ant-wxyz9876") in calls    # canonical env var
    assert ("QSCREEN_PROVIDER", "anthropic") in calls           # forced as the active provider


def test_settings_rejects_local_provider(client):
    r = client.post("/settings", json={"provider": "ollama", "key": "x"})
    assert r.status_code == 400 and "no api key" in r.get_json()["error"].lower()


def test_settings_rejects_unknown_provider(client):
    assert client.post("/settings", json={"provider": "wat", "key": "x"}).status_code == 400


def test_settings_requires_key(client):
    assert client.post("/settings", json={"provider": "openai", "key": "  "}).status_code == 400


def test_settings_refuses_non_loopback(client, monkeypatch):
    monkeypatch.setattr(app_mod.engine, "set_dotenv_value",
                        lambda *a, **k: pytest.fail("must not write a key for a remote client"))
    r = client.post("/settings", json={"provider": "openai", "key": "k"},
                    environ_overrides={"REMOTE_ADDR": "203.0.113.7"})
    assert r.status_code == 403


def test_index_has_settings_panel_and_no_year_field(client):
    h = client.get("/").get_data(as_text=True)
    assert 'id="set_provider"' in h and "saveKey" in h and "DETECTED_PROVIDER" in h
    assert 'name="year" type="number" placeholder="2024" required' not in h   # the required field is gone
    assert 'id="yearfallback"' in h                                           # only the hidden fallback remains


def test_page_inline_javascript_parses(client):
    """The page's inline <script> must be valid JS.

    Regression guard: an unescaped apostrophe in a single-quoted string (PAGE is a
    plain triple-quoted Python string, so `\\'` renders as a bare `'`) once broke the
    whole script — leaving the Settings provider dropdown empty and making Extract
    fall back to a native GET reload instead of POSTing. Validate with `node --check`.
    """
    import shutil, subprocess, tempfile, os
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available to validate page JS")
    html = client.get("/").get_data(as_text=True)
    scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
    assert scripts, "no inline <script> found on the page"
    biggest = max(scripts, key=len)
    tmp = tempfile.NamedTemporaryFile("w", suffix=".js", delete=False)
    try:
        tmp.write(biggest); tmp.close()
        res = subprocess.run([node, "--check", tmp.name], capture_output=True, text=True)
        assert res.returncode == 0, f"page inline JS has a syntax error:\n{res.stderr}"
    finally:
        os.unlink(tmp.name)
