"""Tests for deterministic-first ("Basic") extraction + the MLX provider.

The whole point is that a 270M model never reads numbers: line items come from
the PDF's recovered tables in pure Python. These tests stub or forbid the LLM and
exercise the deterministic helpers, the no-LLM end-to-end path, the Basic/Pro mode
resolution, and the MLX / Gemma "no system role" wiring.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import qscreen_ingest as e


def _pargs(**over):
    base = dict(provider=None, base_url=None, model=None, llm_key=None,
                max_tokens=128, no_json_mode=False, retries=1, timeout=5,
                guided=True, no_guided=False, guided_notes=False, no_llm=False,
                mode=None, basic=False, pro=False,
                symbol="QNBK", sector="conventional_bank", year=2024, period="FY",
                pages_per_chunk=3, overlap=1, no_chunk=False)
    base.update(over)
    return SimpleNamespace(**base)


# A render_window-style string with two TABLES blocks on two pages.
WIN = ("\n===== PAGE 2 =====\nConsolidated Statement of Financial Position\n"
       "[TABLES on page 2]\n-- table 1 --\n"
       "Total assets | 1,000 | 900\n"
       "Loans and advances | 7 | 500 | 450\n"
       "Total equity | 200 | 180\n"
       "\n===== PAGE 3 =====\nConsolidated Income Statement\n"
       "[TABLES on page 3]\n-- table 1 --\n"
       "Net interest income | 50 | 45\n"
       "Profit for the year | (30) | 25\n")


# ── parse_rendered_tables ────────────────────────────────────────────────────

def test_parse_rendered_tables_basic():
    tabs = e.parse_rendered_tables(WIN)
    assert [t["page"] for t in tabs] == [2, 3]
    assert tabs[0]["rows"][0] == ["Total assets", "1,000", "900"]
    assert tabs[0]["rows"][1] == ["Loans and advances", "7", "500", "450"]


def test_parse_rendered_tables_block_boundary():
    # the page-2 block must NOT swallow page-3's rows
    tabs = e.parse_rendered_tables(WIN)
    assert len(tabs[0]["rows"]) == 3 and len(tabs[1]["rows"]) == 2


def test_parse_rendered_tables_none():
    assert e.parse_rendered_tables("just prose, no tables here") == []


def test_parse_rendered_tables_multiple_tables_one_page():
    text = ("[TABLES on page 1]\n-- table 1 --\nA | 1\n-- table 2 --\nB | 2 | 3\n")
    tabs = e.parse_rendered_tables(text)
    assert len(tabs) == 2 and tabs[0]["rows"] == [["A", "1"]] and tabs[1]["rows"] == [["B", "2", "3"]]


# ── _row_to_triplet ──────────────────────────────────────────────────────────

def test_row_triplet_current_prior():
    assert e._row_to_triplet(["Total assets", "1,000", "900"]) == \
        {"label": "Total assets", "current": 1000, "prior": 900, "note_ref": None}


def test_row_triplet_note_ref_column():
    # ≥3 numerics and a small leading int → demote it to note_ref
    assert e._row_to_triplet(["Loans and advances", "7", "1,234", "1,100"]) == \
        {"label": "Loans and advances", "current": 1234, "prior": 1100, "note_ref": "7"}


def test_row_triplet_three_year_columns():
    # first numeric "5,000" is not a note-ref token → no demotion; 3rd column ignored
    t = e._row_to_triplet(["Revenue", "5,000", "4,800", "4,500"])
    assert t["current"] == 5000 and t["prior"] == 4800 and t["note_ref"] is None


def test_row_triplet_two_numerics_ambiguous():
    # only two numerics → keep both as current/prior (never lose a value)
    assert e._row_to_triplet(["X", "12", "34"]) == \
        {"label": "X", "current": 12, "prior": 34, "note_ref": None}


def test_row_triplet_single_number():
    assert e._row_to_triplet(["Cash", "500"]) == \
        {"label": "Cash", "current": 500, "prior": None, "note_ref": None}


def test_row_triplet_header_row_no_numbers():
    t = e._row_to_triplet(["Assets", "", ""])
    assert t["label"] == "Assets" and t["current"] is None and t["prior"] is None


def test_row_triplet_pure_numeric_row_is_none():
    assert e._row_to_triplet(["1", "2"]) is None


def test_row_triplet_bracketed_negative():
    assert e._row_to_triplet(["Impairment", "(56)", "(40)"]) == \
        {"label": "Impairment", "current": -56, "prior": -40, "note_ref": None}


# ── _assign_table_stype ──────────────────────────────────────────────────────

def test_assign_stype_preceding_and_following():
    titles = [("balance_sheet", "BS", 0), ("income_statement", "IS", 500)]
    assert e._assign_table_stype(600, titles)[0] == "income_statement"
    assert e._assign_table_stype(100, titles)[0] == "balance_sheet"
    assert e._assign_table_stype(0, [("cash_flow", "CF", 50)])[0] == "cash_flow"   # following
    assert e._assign_table_stype(10, []) is None


# ── deterministic_statements ─────────────────────────────────────────────────

def test_deterministic_statements_from_tables():
    titles = e.detect_statement_titles(WIN)
    det = e.deterministic_statements(WIN, titles, "2023", "2024")
    assert set(det) == {"balance_sheet", "income_statement"}
    codes = {li["account_code"] for s in det.values() for li in s["line_items"]}
    assert {"BS_TOTAL_ASSETS", "BS_TOTAL_EQUITY", "BS_LOANS",
            "IS_NET_INTEREST", "IS_NET_INCOME"} <= codes
    assert all(li["basis"] == "parsed" for s in det.values() for li in s["line_items"])
    ta = next(li for li in det["balance_sheet"]["line_items"]
              if li["label_verbatim"] == "Total assets")
    assert ta["value"] == 1000 and ta["comparatives"] == [{"period_label": "2023", "value": 900}]
    loans = next(li for li in det["balance_sheet"]["line_items"]
                 if li["label_verbatim"] == "Loans and advances")
    assert loans["note_ref"] == "7"


def test_deterministic_statements_empty_without_tables():
    assert e.deterministic_statements("no tables", e.detect_statement_titles("no tables"),
                                      "2023", "2024") == {}


# ── end-to-end (no LLM) ──────────────────────────────────────────────────────

def _table_pages():
    return [
        {"num": 1, "text": "Independent Auditor's Report. In our opinion the financial statements "
                           "present fairly ... this is an unqualified opinion. "
                           "(Amounts in thousands of Qatari Riyals)"},
        {"num": 2, "text": "Statement of Financial Position\n[TABLES on page 2]\n-- table 1 --\n"
                           "Total assets | 1,000 | 900\nTotal equity | 200 | 180\n"},
        {"num": 3, "text": "Income Statement\n[TABLES on page 3]\n-- table 1 --\n"
                           "Net interest income | 50 | 45\nProfit for the year | 30 | 25\n"},
    ]


def test_no_llm_end_to_end_conforming(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("call_llm must not be called with --no-llm")
    monkeypatch.setattr(e, "call_llm", boom)

    out = e.extract_filing(_table_pages(), _pargs(no_llm=True, guided=True, guided_notes=True))
    assert e.validate_filing(out) == []
    codes = {li["account_code"] for s in out["statements"] for li in s["line_items"]}
    assert {"BS_TOTAL_ASSETS", "BS_TOTAL_EQUITY", "IS_NET_INTEREST", "IS_NET_INCOME"} <= codes
    assert out["metadata"]["unit_scale"] == 1000
    assert out["audit"]["opinion_type"] == "unqualified"        # read deterministically from text
    assert out["notes"] == []
    assert all(li["basis"] == "parsed" for s in out["statements"] for li in s["line_items"])
    assert any("parsed from tables" in w for w in out["extraction_quality"]["warnings"])


def test_deterministic_first_skips_llm_when_tables_present(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("LLM should not be called when a table backs the statement")
    monkeypatch.setattr(e, "call_llm", boom)
    out = e.extract_filing(_table_pages(), _pargs(no_llm=False, guided=True))
    assert any(s["type"] == "balance_sheet" for s in out["statements"])
    assert e.validate_filing(out) == []


def test_falls_back_to_llm_when_no_tables(monkeypatch):
    # plain-text statement (no TABLES block) must still hit the model path
    calls = {"n": 0}

    def fake(messages, args):
        calls["n"] += 1
        return '{"rows":[{"label":"Total assets","current":1,"prior":null}]}'
    monkeypatch.setattr(e, "call_llm", fake)
    pages = [{"num": 1, "text": "Statement of Financial Position\nTotal assets 1\n"}]
    out = e.extract_filing(pages, _pargs(no_llm=False, guided=True))
    assert calls["n"] >= 1
    assert any(li["basis"] == "llm" for s in out["statements"] for li in s["line_items"])


# ── Basic / Pro mode resolution ──────────────────────────────────────────────

def test_apply_mode_basic_and_pro():
    a = _pargs(mode="basic", guided=False, no_guided=False)
    e.apply_mode(a)
    assert a.guided is True
    b = _pargs(mode="pro", guided=False, no_guided=False)
    e.apply_mode(b)
    assert b.no_guided is True


def test_apply_mode_no_llm_implies_basic():
    a = _pargs(no_llm=True, mode=None, guided=False, no_guided=False)
    e.apply_mode(a)
    assert a.guided is True


def test_apply_mode_auto_leaves_flags(monkeypatch):
    a = _pargs(mode="auto", guided=False, no_guided=False)
    e.apply_mode(a)
    assert a.guided is False and a.no_guided is False         # resolve_guided decides later


# ── MLX provider + Gemma no-system-role ──────────────────────────────────────

def test_mlx_provider_registered():
    p = e.PROVIDERS["mlx"]
    assert p["local"] is True and p["kind"] == "openai" and p["no_system"] is True
    assert p["base_url"] == "http://localhost:8080/v1"
    assert p["default_model"] == "mlx-community/gemma-3-270m-it-4bit"
    assert p["key_url"].startswith("https://")
    assert e.canonical_provider("apple") == "mlx" and e.canonical_provider("mlx-lm") == "mlx"


def test_resolve_mlx_without_key(monkeypatch):
    for k in ("MLX_API_KEY", "LLM_API_KEY", "QSCREEN_BASE_URL", "LLM_BASE_URL", "QSCREEN_MODEL"):
        monkeypatch.delenv(k, raising=False)
    cfg = e.resolve_provider(_pargs(provider="mlx"))
    assert cfg["name"] == "mlx" and cfg["key"] == "local"
    assert cfg["no_system"] is True and cfg["local"] is True
    assert e.resolve_guided(_pargs(guided=False, no_guided=False), cfg) is True   # local → Basic


def test_merge_system_into_user():
    out = e._merge_system_into_user([{"role": "system", "content": "S1"},
                                     {"role": "system", "content": "S2"},
                                     {"role": "user", "content": "U"}])
    assert out == [{"role": "user", "content": "S1\n\nS2\n\nU"}]
    # no system → unchanged
    same = e._merge_system_into_user([{"role": "user", "content": "U"}])
    assert same == [{"role": "user", "content": "U"}]
    # no user → system becomes the user turn
    only = e._merge_system_into_user([{"role": "system", "content": "S"}])
    assert only == [{"role": "user", "content": "S"}]


def test_openai_request_merges_when_no_system():
    msgs = [{"role": "system", "content": "SYS"}, {"role": "user", "content": "U"}]
    cfg = {"base_url": "http://localhost:8080/v1", "model": "g", "key": "local", "no_system": True}
    _url, _h, payload, _x = e._openai_request(msgs, cfg, _pargs(max_tokens=10))
    assert all(m["role"] != "system" for m in payload["messages"])
    assert payload["messages"][0]["content"].startswith("SYS")


def test_openai_request_unchanged_without_no_system():
    msgs = [{"role": "system", "content": "S"}, {"role": "user", "content": "U"}]
    cfg = {"base_url": "https://api.openai.com/v1", "model": "gpt", "key": "k"}   # no no_system
    _url, _h, payload, _x = e._openai_request(msgs, cfg, _pargs(max_tokens=10))
    assert payload["messages"] == msgs                       # strict no-op
    assert payload["response_format"] == {"type": "json_object"}


# ── schema enforcement ───────────────────────────────────────────────────────

def test_ollama_schema_uses_native_format(monkeypatch):
    for k in ("OLLAMA_API_KEY", "LLM_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    cfg = e.resolve_provider(_pargs(provider="ollama"))
    args = _pargs(max_tokens=10)
    args._schema = e._ROWS_SCHEMA
    _u, _h, payload, _x = e._openai_request([{"role": "user", "content": "x"}], cfg, args)
    assert payload["format"] == e._ROWS_SCHEMA and "response_format" not in payload


def test_lmstudio_schema_uses_response_format(monkeypatch):
    for k in ("LMSTUDIO_API_KEY", "LLM_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    cfg = e.resolve_provider(_pargs(provider="lmstudio"))
    args = _pargs(max_tokens=10)
    args._schema = e._AUDIT_SCHEMA
    _u, _h, payload, _x = e._openai_request([{"role": "user", "content": "x"}], cfg, args)
    assert payload["response_format"]["type"] == "json_schema"
    assert payload["response_format"]["json_schema"]["schema"] == e._AUDIT_SCHEMA


def test_mlx_ignores_schema(monkeypatch):
    for k in ("MLX_API_KEY", "LLM_API_KEY", "QSCREEN_MODEL"):
        monkeypatch.delenv(k, raising=False)
    cfg = e.resolve_provider(_pargs(provider="mlx"))
    args = _pargs(max_tokens=10)
    args._schema = e._ROWS_SCHEMA
    _u, _h, payload, _x = e._openai_request([{"role": "user", "content": "x"}], cfg, args)
    assert "format" not in payload                           # MLX gets no schema field
    assert payload.get("response_format") == {"type": "json_object"}
