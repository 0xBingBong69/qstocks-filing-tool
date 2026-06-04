"""Security regression tests for the browser app and chart rendering.

Covers the hardening of: reflected/stored XSS in the result panel (escaping wired
into the page), traceback info-disclosure on /extract, Content-Disposition
filename injection, source_file path stripping, and SVG label/title escaping.
"""
from __future__ import annotations

import io

import pytest

import qscreen_app as app_mod
import qscreen_charts as charts


@pytest.fixture
def client():
    app_mod.app.config["TESTING"] = True
    return app_mod.app.test_client()


def _statement_filing(symbol="QNBK"):
    li = [{"account_code": c, "label_verbatim": c, "value": v,
           "comparatives": [{"period_label": "2022", "value": v}]}
          for c, v in [("IS_NET_INCOME", 15000), ("BS_TOTAL_EQUITY", 100000)]]
    return {"metadata": {"symbol": symbol, "fiscal_year": 2023, "fiscal_period": "FY", "currency": "QAR"},
            "statements": [{"type": "income_statement", "title": "IS", "period_label": "2023",
                            "verbatim_text": "x", "line_items": li}]}


# ── _safe_filename helper ────────────────────────────────────────────────────

def test_safe_filename_strips_dangerous_chars():
    assert app_mod._safe_filename('A" ;\r\nSet-Cookie: x=y') == "A_____Set-Cookie__x_y"
    assert app_mod._safe_filename("../../etc/passwd") == "etc_passwd"
    assert app_mod._safe_filename("") == "filing"
    assert app_mod._safe_filename(None) == "filing"
    out = app_mod._safe_filename("QNBK")
    assert out == "QNBK"


# ── Content-Disposition header injection ─────────────────────────────────────

EVIL_SYM = 'EVIL" ;\r\nSet-Cookie: pwn=1'


def test_workbook_content_disposition_sanitized(client):
    r = client.post("/workbook", json={"filing": _statement_filing(symbol=EVIL_SYM)})
    assert r.status_code == 200
    cd = r.headers["Content-Disposition"]
    assert "\r" not in cd and "\n" not in cd          # no header/CRLF injection
    assert cd.count('"') == 2                          # no quote break-out of filename="..."
    assert "Set-Cookie" not in cd.split("filename=")[0]  # nothing injected before the filename


def test_export_csv_content_disposition_sanitized(client):
    r = client.post("/export.csv", json={"filing": _statement_filing(symbol=EVIL_SYM)})
    assert r.status_code == 200
    cd = r.headers["Content-Disposition"]
    assert "\r" not in cd and "\n" not in cd and cd.count('"') == 2


# ── /extract must not leak a traceback, and must strip the upload path ───────

def _cfg():
    return {"name": "openai", "model": "gpt-4o", "local": False,
            "base_url": "https://api.openai.com/v1", "kind": "openai", "key": "k"}


def test_extract_error_does_not_leak_traceback(client, monkeypatch):
    monkeypatch.setattr(app_mod.engine, "resolve_provider", lambda args: _cfg())
    monkeypatch.setattr(app_mod.engine, "pdf_to_pages", lambda path: ([{"num": 1, "text": "x"}], "sha"))

    def boom(pages, args):
        raise ValueError('parse failed near /home/secret/path line 42')
    monkeypatch.setattr(app_mod.engine, "extract_filing", boom)

    r = client.post("/extract", data={"symbol": "QNBK", "subsector": "Commercial Bank",
                                       "year": "2024", "period": "FY",
                                       "pdf": (io.BytesIO(b"%PDF-1.4 fake"), "x.pdf")},
                    content_type="multipart/form-data")
    assert r.status_code == 500
    body = r.get_json()
    assert "detail" not in body                        # traceback no longer returned
    assert "Traceback" not in body["error"] and 'File "' not in body["error"]


def test_extract_strips_path_from_source_file(client, monkeypatch):
    monkeypatch.setattr(app_mod.engine, "resolve_provider", lambda args: _cfg())
    monkeypatch.setattr(app_mod.engine, "pdf_to_pages", lambda path: ([{"num": 1, "text": "x"}], "sha"))
    monkeypatch.setattr(app_mod.engine, "extract_filing", lambda pages, args: _statement_filing())

    r = client.post("/extract", data={"symbol": "QNBK", "subsector": "Commercial Bank",
                                       "year": "2024", "period": "FY",
                                       "pdf": (io.BytesIO(b"%PDF-1.4 fake"), "../../../etc/passwd.pdf")},
                    content_type="multipart/form-data")
    assert r.status_code == 200, r.get_json()
    src = r.get_json()["filing"]["metadata"]["source_file"]
    assert src == "passwd.pdf" and "/" not in src and "\\" not in src


# ── The page must escape dynamic strings before innerHTML ────────────────────

def test_index_escapes_result_panel(client):
    html = client.get("/").get_data(as_text=True)
    # the high-value sink: summary + problems from a (possibly malicious) filing
    assert "esc(data.summary)" in html
    assert "data.problems.map(esc)" in html
    assert "esc(data.error" in html
    # the old vulnerable patterns must be gone
    assert "'+ data.summary +'" not in html and "+ data.summary +" not in html
    assert "'<span class=\"err\">'+e+'</span>'" not in html      # err sink now wraps esc(e)


# ── SVG charts escape labels / title (defense-in-depth) ──────────────────────

def test_bars_escapes_labels_and_title():
    svg = charts.bars(["<script>x</script>", "2024"], [10, 20], title="<b>Profit</b>")
    assert "<script>" not in svg and "&lt;script&gt;" in svg
    assert "<b>Profit</b>" not in svg and "&lt;b&gt;Profit&lt;/b&gt;" in svg
