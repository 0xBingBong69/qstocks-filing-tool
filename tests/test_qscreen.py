"""Offline test suite for the QScreen filing tool — no network, no API key.

Covers the contract validator, the LLM-output normalizer, the lossless
cross-window merge, page windowing, the hardened JSON parser, exports, the
batch manifest reader, OCR fall-through, and the LLM/upload HTTP wrappers
(with requests stubbed).
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import qscreen_ingest as e


# ── fixtures / helpers ───────────────────────────────────────────────────────

def good_filing() -> dict:
    f = e.empty_filing()
    f["metadata"].update({"symbol": "QNBK", "sector": "conventional_bank",
                          "fiscal_year": 2023, "fiscal_period": "FY", "unit_scale": 1000})
    f["audit"].update({"opinion_type": "unqualified", "verbatim_text": "In our opinion …"})
    f["statements"].append({"type": "income_statement", "title": "Income", "period_label": "2023",
                            "verbatim_text": "NII 1",
                            "line_items": [{"account_code": "IS_NET_INTEREST", "label_verbatim": "NII",
                                            "value": 1, "note_ref": "24", "depth": 0, "is_subtotal": False}]})
    f["notes"].append({"number": "27", "title": "Contingencies", "category": "contingent_liabilities",
                       "structured": {}, "verbatim_text": "…"})
    return f


def llm_args(**over):
    base = dict(symbol="QNBK", sector="conventional_bank", year=2023, period="FY",
                provider="openrouter", base_url=None, model=None, max_tokens=128,
                timeout=5, retries=3, no_json_mode=False, llm_key="sk-test",
                pages_per_chunk=12, overlap=1, no_chunk=False)
    base.update(over)
    return SimpleNamespace(**base)


# ── contract validation ──────────────────────────────────────────────────────

def test_validate_accepts_good():
    assert e.validate_filing(good_filing()) == []


def test_validate_rejects_empty_statements():
    f = good_filing()
    f["statements"] = []
    assert any("statements: empty" in p for p in e.validate_filing(f))


def test_validate_rejects_unknown_opinion():
    f = good_filing()
    f["audit"]["opinion_type"] = "great"
    assert any("opinion_type" in p for p in e.validate_filing(f))


def test_validate_flags_opinion_without_verbatim():
    f = good_filing()
    f["audit"]["verbatim_text"] = ""
    assert any("verbatim_text" in p for p in e.validate_filing(f))


def test_validate_flags_unknown_account_code():
    f = good_filing()
    f["statements"][0]["line_items"][0]["account_code"] = "IS_MADE_UP"
    assert any("unknown" in p for p in e.validate_filing(f))


def test_validate_flags_bad_unit_scale():
    f = good_filing()
    f["metadata"]["unit_scale"] = 100
    assert any("unit_scale" in p for p in e.validate_filing(f))


def test_validate_flags_lossy_line_item():
    f = good_filing()
    f["statements"][0]["line_items"][0]["label_verbatim"] = ""
    assert any("label_verbatim" in p for p in e.validate_filing(f))


# ── normalization ────────────────────────────────────────────────────────────

def test_normalize_aliases_and_unknown_code():
    drifted = {
        "metadata": {"ticker": "QIBK", "company": "Qatar Islamic Bank", "sector": "Islamic Bank",
                     "reporting_currency": "QAR", "framework": "AAOIFI", "unit_scale": 1000},
        "audit": {"opinion_type": "unqualified", "opinion_text": "In our opinion …",
                  "key_audit_matters": [{"title": "ECL", "description": "judgemental ECL"}],
                  "emphasis_of_matter": "one matter"},
        "statements": [{"type": "income_statement", "period": "year_ended_2024", "verbatim_text": "…",
                        "line_items": [{"label_verbatim": "x", "value": 1},
                                       {"account_code": "IS_OTHER_COMPREHENSIVE_INCOME",
                                        "label_verbatim": "OCI", "value": 2}]}],
        "notes": [], "extraction_quality": {},
    }
    n = e.normalize_filing(drifted)
    assert n["metadata"]["symbol"] == "QIBK"
    assert n["metadata"]["company_name"] == "Qatar Islamic Bank"
    assert n["metadata"]["sector"] == "islamic_bank"
    assert n["metadata"]["currency"] == "QAR"
    assert n["metadata"]["reporting_framework"] == "AAOIFI"
    assert n["audit"]["verbatim_text"]                       # opinion_text → verbatim_text
    assert n["audit"]["key_audit_matters"][0]["text"] == "judgemental ECL"
    assert n["audit"]["emphasis_of_matter"] == ["one matter"]  # str coerced to list
    assert n["statements"][0]["period_label"] == "year_ended_2024"
    assert n["statements"][0]["line_items"][1]["account_code"] is None  # unknown → null
    assert any("IS_OTHER_COMPREHENSIVE_INCOME" in u
               for u in n["extraction_quality"]["unmapped_labels"])


@pytest.mark.parametrize("raw,expect", [
    ("Islamic Bank", "islamic_bank"), ("commercial-bank", "conventional_bank"),
    ("Takaful", "insurance"), ("Petrochemicals industrials", "industrial"),
    ("Real Estate", "other"), ("", None),
])
def test_normalize_sector(raw, expect):
    assert e._normalize_sector(raw) == expect


@pytest.mark.parametrize("raw,expect", [
    (1000, 1000), ("in thousands", 1000), ("QAR millions", 1000000), ("nonsense", None),
])
def test_normalize_unit_scale(raw, expect):
    assert e._normalize_unit_scale(raw) == expect


# ── lossless cross-window merge ──────────────────────────────────────────────

def test_merge_combines_windows():
    a = e.empty_filing(); a["audit"].update({"opinion_type": "unqualified", "verbatim_text": "op …"})
    b = e.empty_filing(); b["statements"].append(
        {"type": "balance_sheet", "verbatim_text": "BS …",
         "line_items": [{"label_verbatim": "Total assets", "value": 9, "account_code": "BS_TOTAL_ASSETS"}]})
    c = e.empty_filing(); c["notes"].append(
        {"number": "5", "title": "Sukuk", "category": "sukuk_islamic", "structured": {}, "verbatim_text": "sukuk …"})
    m = e.merge_filings([a, b, c])
    assert m["audit"]["opinion_type"] == "unqualified"
    assert m["statements"] and m["notes"]


def test_merge_unions_split_statement_lossless():
    w1 = e.empty_filing(); w2 = e.empty_filing()
    w1["statements"].append({"type": "income_statement", "title": "IS", "period_label": "2024",
        "verbatim_text": "the much longer first-window verbatim block",
        "line_items": [
            {"label_verbatim": "Revenue", "value": 10, "account_code": "IS_REVENUE"},
            {"label_verbatim": "Net interest", "value": 5, "account_code": None}]})
    w2["statements"].append({"type": "income_statement", "title": "IS", "period_label": "2024",
        "verbatim_text": "short",
        "line_items": [
            {"label_verbatim": "Net interest", "value": 5, "account_code": "IS_NET_INTEREST"},  # overlap dup
            {"label_verbatim": "Net income", "value": 3, "account_code": "IS_NET_INCOME"}]})     # split row
    st = e.merge_filings([w1, w2])["statements"][0]
    labels = [li["label_verbatim"] for li in st["line_items"]]
    assert labels == ["Revenue", "Net interest", "Net income"]          # no loss, no dup
    ni = next(li for li in st["line_items"] if li["label_verbatim"] == "Net interest")
    assert ni["account_code"] == "IS_NET_INTEREST"                      # null upgraded from dup
    assert st["verbatim_text"].startswith("the much longer")           # longest verbatim kept


def test_merge_dedups_kams_and_aggregates_quality():
    w1 = e.empty_filing()
    w1["audit"].update({"opinion_type": "unqualified", "verbatim_text": "opinion",
                        "key_audit_matters": [{"title": "ECL", "text": "aaa"}]})
    w1["extraction_quality"] = {"confidence": 0.9, "warnings": ["w1"], "unmapped_labels": ["u1"]}
    w2 = e.empty_filing()
    w2["audit"].update({"key_audit_matters": [{"title": "ECL", "text": "aaa"}]})  # duplicate KAM
    w2["extraction_quality"] = {"confidence": 0.5, "warnings": ["w1", "w2"], "unmapped_labels": ["u2"]}
    m = e.merge_filings([w1, w2])
    assert len(m["audit"]["key_audit_matters"]) == 1
    assert m["extraction_quality"]["confidence"] == 0.5                 # min
    assert m["extraction_quality"]["warnings"] == ["w1", "w2"]          # dedup + sorted
    assert m["extraction_quality"]["unmapped_labels"] == ["u1", "u2"]


# ── page windowing ───────────────────────────────────────────────────────────

def test_page_windows_overlap_and_coverage():
    pages = [{"num": i, "text": str(i)} for i in range(1, 8)]   # 7 pages
    wins = e.page_windows(pages, size=3, overlap=1)             # step 2, stops at the last full window
    assert [(w[0]["num"], w[-1]["num"]) for w in wins] == [(1, 3), (3, 5), (5, 7)]


def test_page_windows_no_chunk_when_size_zero():
    pages = [{"num": i, "text": str(i)} for i in range(1, 5)]
    assert e.page_windows(pages, size=0, overlap=0) == [pages]


# ── hardened JSON parsing ────────────────────────────────────────────────────

def test_parse_plain_object():
    assert e.parse_llm_json('{"a": 1}') == {"a": 1}


def test_parse_strips_code_fences():
    assert e.parse_llm_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_parse_tolerates_trailing_prose_and_inner_braces():
    assert e.parse_llm_json('{"a": "x}y", "b": {"n": 2}} thanks!')["b"]["n"] == 2


def test_parse_handles_escaped_quotes():
    assert e.parse_llm_json(r'{"a": "she said \"hi\" {"}')["a"] == 'she said "hi" {'


def test_parse_no_object_raises():
    with pytest.raises(ValueError):
        e.parse_llm_json("no json here")


# ── exports ──────────────────────────────────────────────────────────────────

def test_flatten_and_csv_export(tmp_path):
    f = good_filing()
    rows = e.flatten_line_items(f)
    assert rows and rows[0]["account_code"] == "IS_NET_INTEREST"
    out = tmp_path / "x.csv"
    n = e.export_csv(f, str(out))
    lines = out.read_text(encoding="utf-8").splitlines()
    assert n == 1
    assert lines[0].split(",")[0] == "statement_type"


def test_xlsx_export(tmp_path):
    out = tmp_path / "x.xlsx"
    n = e.export_xlsx(good_filing(), str(out))
    from openpyxl import load_workbook
    ws = load_workbook(str(out)).active
    assert n == 1
    assert ws.max_row == 2 and ws.max_column == len(e.EXPORT_COLUMNS)


# ── batch manifest ───────────────────────────────────────────────────────────

def test_read_manifest_ok(tmp_path):
    p = tmp_path / "m.csv"
    p.write_text("pdf,symbol,sector,year,period\na.pdf,QIBK,Islamic Bank,2024,FY\n"
                 "b.pdf,QNBK,conventional_bank,2023,\n", encoding="utf-8")
    rows = e.read_manifest(str(p))
    assert [r["symbol"] for r in rows] == ["QIBK", "QNBK"]


def test_read_manifest_missing_column(tmp_path):
    p = tmp_path / "m.csv"
    p.write_text("pdf,symbol,year\na.pdf,QIBK,2024\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        e.read_manifest(str(p))


def test_read_manifest_empty(tmp_path):
    p = tmp_path / "m.csv"
    p.write_text("pdf,symbol,sector,year\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        e.read_manifest(str(p))


# ── OCR fall-through (pdfplumber stubbed; OCR deps absent) ────────────────────

class _FakePage:
    def __init__(self, text): self._t = text
    def extract_text(self): return self._t
    def extract_tables(self): return []


class _FakePDF:
    def __init__(self, texts): self.pages = [_FakePage(t) for t in texts]
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _stub_pdf(monkeypatch, texts, tmp_path):
    import pdfplumber
    monkeypatch.setattr(pdfplumber, "open", lambda path: _FakePDF(texts))
    pdf = tmp_path / "f.pdf"
    pdf.write_bytes(b"%PDF-1.4 stub")
    return str(pdf)


def test_ocr_auto_warns_when_unavailable(monkeypatch, tmp_path, capsys):
    path = _stub_pdf(monkeypatch, ["full page of text " * 5, ""], tmp_path)
    monkeypatch.setattr(e, "_ocr_pages", lambda p, nums: (_ for _ in ()).throw(e.OcrUnavailable("no deps")))
    pages, sha = e.pdf_to_pages(path, ocr_mode="auto")
    assert len(pages) == 2 and pages[1]["text"] == ""        # unchanged, no crash
    assert "likely" in capsys.readouterr().out.lower()
    assert len(sha) == 64


def test_ocr_always_recovers_text(monkeypatch, tmp_path):
    path = _stub_pdf(monkeypatch, ["", ""], tmp_path)
    monkeypatch.setattr(e, "_ocr_pages", lambda p, nums: {n: f"ocr-page-{n}" for n in nums})
    pages, _ = e.pdf_to_pages(path, ocr_mode="always")
    assert "ocr-page-1" in pages[0]["text"] and "ocr-page-2" in pages[1]["text"]


def test_ocr_always_raises_when_unavailable(monkeypatch, tmp_path):
    path = _stub_pdf(monkeypatch, [""], tmp_path)
    monkeypatch.setattr(e, "_ocr_pages", lambda p, nums: (_ for _ in ()).throw(e.OcrUnavailable("no deps")))
    with pytest.raises(SystemExit):
        e.pdf_to_pages(path, ocr_mode="always")


# ── extract_filing orchestration (call_llm stubbed) ──────────────────────────

def test_extract_single_pass(monkeypatch):
    payload = json.dumps({
        "metadata": {"ticker": "QNBK"},
        "audit": {"opinion_type": "unqualified", "verbatim_text": "op"},
        "statements": [{"type": "balance_sheet", "verbatim_text": "BS",
                        "line_items": [{"label_verbatim": "Total assets", "value": 1,
                                        "account_code": "BS_TOTAL_ASSETS"}]}],
        "notes": [], "extraction_quality": {"confidence": 0.8},
    })
    monkeypatch.setattr(e, "call_llm", lambda messages, args: payload)
    pages = [{"num": 1, "text": "page one"}]
    out = e.extract_filing(pages, llm_args(no_chunk=True))
    assert out["metadata"]["symbol"] == "QNBK"               # normalized alias
    assert out["statements"][0]["type"] == "balance_sheet"


def test_extract_windowed_merges(monkeypatch):
    def fake(messages, args):
        # Different statement per window so the merge must combine them.
        body = messages[1]["content"]
        if "PAGE 1" in body:
            return json.dumps({"metadata": {}, "audit": {"opinion_type": "unknown", "verbatim_text": ""},
                               "statements": [{"type": "income_statement", "verbatim_text": "IS",
                                               "line_items": [{"label_verbatim": "Rev", "value": 1,
                                                               "account_code": "IS_REVENUE"}]}],
                               "notes": [], "extraction_quality": {}})
        return json.dumps({"metadata": {}, "audit": {"opinion_type": "unknown", "verbatim_text": ""},
                           "statements": [{"type": "balance_sheet", "verbatim_text": "BS",
                                           "line_items": [{"label_verbatim": "TA", "value": 2,
                                                           "account_code": "BS_TOTAL_ASSETS"}]}],
                           "notes": [], "extraction_quality": {}})
    monkeypatch.setattr(e, "call_llm", fake)
    pages = [{"num": i, "text": f"content {i}"} for i in range(1, 6)]
    out = e.extract_filing(pages, llm_args(no_chunk=False, pages_per_chunk=2, overlap=1))
    types = {s["type"] for s in out["statements"]}
    assert {"income_statement", "balance_sheet"} <= types


def test_extract_windowed_skips_unparseable(monkeypatch):
    calls = {"n": 0}
    def fake(messages, args):
        calls["n"] += 1
        if calls["n"] == 1:
            return "not json at all"
        return json.dumps({"metadata": {}, "audit": {"opinion_type": "unknown", "verbatim_text": ""},
                           "statements": [{"type": "cash_flow", "verbatim_text": "CF",
                                           "line_items": [{"label_verbatim": "OCF", "value": 1,
                                                           "account_code": "CF_OCF"}]}],
                           "notes": [], "extraction_quality": {}})
    monkeypatch.setattr(e, "call_llm", fake)
    pages = [{"num": i, "text": f"c{i}"} for i in range(1, 6)]
    out = e.extract_filing(pages, llm_args(no_chunk=False, pages_per_chunk=2, overlap=1))
    assert any(s["type"] == "cash_flow" for s in out["statements"])  # survived a bad window


# ── HTTP wrappers (requests stubbed) ─────────────────────────────────────────

class _Resp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload or {}
        self.text = text
    def json(self): return self._payload
    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def test_call_llm_success(monkeypatch):
    import requests
    ok = _Resp(200, {"choices": [{"message": {"content": '{"ok": 1}'}}]})
    monkeypatch.setattr(requests, "post", lambda *a, **k: ok)
    assert e.call_llm([{"role": "user", "content": "hi"}], llm_args()) == '{"ok": 1}'


def test_call_llm_403_is_actionable(monkeypatch):
    import requests
    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp(403, text="Forbidden"))
    with pytest.raises(SystemExit) as ei:
        e.call_llm([{"role": "user", "content": "hi"}], llm_args())
    assert "network policy" in str(ei.value)


def test_call_llm_bad_model_is_actionable(monkeypatch):
    import requests
    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp(400, text="model not found"))
    with pytest.raises(SystemExit) as ei:
        e.call_llm([{"role": "user", "content": "hi"}], llm_args(model="bogus/model"))
    assert "model" in str(ei.value).lower()


def test_call_llm_retries_then_succeeds(monkeypatch):
    import requests
    seq = [_Resp(503, text="busy"), _Resp(200, {"choices": [{"message": {"content": "ok"}}]})]
    monkeypatch.setattr(requests, "post", lambda *a, **k: seq.pop(0))
    monkeypatch.setattr(e.time, "sleep", lambda *_: None)
    assert e.call_llm([{"role": "user", "content": "hi"}], llm_args(retries=3)) == "ok"


def test_upload_filing_builds_request(monkeypatch):
    import requests
    captured = {}
    def fake_post(url, headers=None, json=None, timeout=None):
        captured.update(url=url, headers=headers, json=json)
        return _Resp(200, {"id": "f1"})
    monkeypatch.setattr(requests, "post", fake_post)
    out = e.upload_filing({"x": 1}, SimpleNamespace(api_url="https://qscreen.app/", token="tok"))
    assert out == {"id": "f1"}
    assert captured["url"] == "https://qscreen.app/api/v1/ingest/filing"
    assert captured["headers"]["Authorization"] == "Bearer tok"


# ── self-test entrypoint still green ─────────────────────────────────────────

def test_self_test_passes():
    assert e.run_self_test() == 0
