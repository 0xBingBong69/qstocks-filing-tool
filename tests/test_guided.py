"""Tests for local-AI providers and the guided (small-model) extractor.

All offline: the LLM is stubbed, so these exercise the deterministic 'rules in
code' (title detection, label→code mapping, unit scale, number parsing) and the
tiny per-table asks that let a 2-bit-Gemma-class model fill the same contract.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import qscreen_ingest as e


_PROVIDER_ENV = ("MINIMAX_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY",
                 "ANTHROPIC_API_KEY", "MOONSHOT_API_KEY", "KIMI_API_KEY",
                 "OLLAMA_API_KEY", "LMSTUDIO_API_KEY", "LLAMACPP_API_KEY",
                 "JAN_API_KEY", "GPT4ALL_API_KEY",
                 "QSCREEN_PROVIDER", "LLM_PROVIDER", "QSCREEN_MODEL", "LLM_API_KEY",
                 "QSCREEN_BASE_URL", "LLM_BASE_URL", "QSCREEN_GUIDED")


@pytest.fixture
def clean_env(monkeypatch):
    for k in _PROVIDER_ENV:
        monkeypatch.delenv(k, raising=False)


def _pargs(**over):
    base = dict(provider=None, base_url=None, model=None, llm_key=None,
                max_tokens=128, no_json_mode=False, retries=1, timeout=5,
                guided=False, no_guided=False, guided_notes=False,
                symbol="QNBK", sector="conventional_bank", year=2024, period="FY",
                pages_per_chunk=3, overlap=1, no_chunk=False)
    base.update(over)
    return SimpleNamespace(**base)


# ── local provider registry ──────────────────────────────────────────────────

LOCALS = ("ollama", "lmstudio", "llamacpp", "jan", "gpt4all")


def test_local_providers_registered():
    for name in LOCALS:
        p = e.PROVIDERS[name]
        assert p.get("local") is True, name
        assert p["kind"] == "openai", name
        assert p["base_url"].startswith("http://localhost:"), name
        assert p["key_url"].startswith("https://"), name      # download/docs link
        assert p["default_model"], name
        assert e.is_local_provider(name)
    assert not e.is_local_provider("openai")
    assert e.PROVIDERS["ollama"]["base_url"] == "http://localhost:11434/v1"


def test_local_aliases():
    assert e.canonical_provider("local") == "ollama"
    assert e.canonical_provider("llama.cpp") == "llamacpp"
    assert e.canonical_provider("lm-studio") == "lmstudio"


def test_list_providers_shows_local_runtimes():
    txt = e.list_providers()
    for name in LOCALS:
        assert name in txt
    assert "NO API key" in txt and "localhost:11434" in txt


# ── resolve_provider: local needs no key ─────────────────────────────────────

def test_resolve_local_without_key(clean_env):
    cfg = e.resolve_provider(_pargs(provider="ollama"))
    assert cfg["name"] == "ollama" and cfg["local"] is True
    assert cfg["base_url"] == "http://localhost:11434/v1"
    assert cfg["model"] == "gemma2:2b" and cfg["key"] == "local"


def test_resolve_local_model_override(clean_env):
    cfg = e.resolve_provider(_pargs(provider="lmstudio", model="gemma-2-2b-it"))
    assert cfg["model"] == "gemma-2-2b-it" and cfg["local"] is True


def test_resolve_local_base_url_env_override(clean_env, monkeypatch):
    monkeypatch.setenv("QSCREEN_BASE_URL", "http://192.168.1.50:11434/v1")
    cfg = e.resolve_provider(_pargs(provider="ollama"))
    assert cfg["base_url"] == "http://192.168.1.50:11434/v1"


def test_resolve_custom_keyless_now_allowed(clean_env):
    # A custom OpenAI-compatible URL (often a local server) no longer forces a key,
    # but isn't presumed small, so it does NOT auto-enable guided.
    cfg = e.resolve_provider(_pargs(provider="custom", base_url="http://localhost:8000/v1", model="m"))
    assert cfg["key"] == "local" and cfg["local"] is False and cfg["base_url"].endswith(":8000/v1")


def test_resolve_custom_via_env_base_url(clean_env, monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "http://localhost:5000/v1")
    cfg = e.resolve_provider(_pargs(provider="custom", model="m"))
    assert cfg["base_url"] == "http://localhost:5000/v1"


def test_cloud_still_requires_key(clean_env):
    with pytest.raises(SystemExit):
        e.resolve_provider(_pargs(provider="openai"))


# ── resolve_guided ───────────────────────────────────────────────────────────

def test_resolve_guided_auto_on_for_local(clean_env):
    assert e.resolve_guided(_pargs(), {"local": True}) is True
    assert e.resolve_guided(_pargs(), {"local": False}) is False


def test_resolve_guided_explicit_flags(clean_env):
    assert e.resolve_guided(_pargs(guided=True), {"local": False}) is True
    assert e.resolve_guided(_pargs(no_guided=True), {"local": True}) is False


def test_resolve_guided_env(clean_env, monkeypatch):
    monkeypatch.setenv("QSCREEN_GUIDED", "1")
    assert e.resolve_guided(_pargs(), {"local": False}) is True
    monkeypatch.setenv("QSCREEN_GUIDED", "off")
    assert e.resolve_guided(_pargs(), {"local": True}) is False


# ── deterministic rules: label → code ────────────────────────────────────────

def test_label_to_code_specificity_and_scope():
    assert e.map_label_to_code("Net interest income", "income_statement") == "IS_NET_INTEREST"
    assert e.map_label_to_code("Interest income", "income_statement") == "IS_INTEREST_INCOME"
    assert e.map_label_to_code("Total liabilities and equity", "balance_sheet") == "BS_TLOE"
    assert e.map_label_to_code("Total liabilities", "balance_sheet") == "BS_TOTAL_LIABILITIES"
    assert e.map_label_to_code("Profit for the year", "income_statement") == "IS_NET_INCOME"
    assert e.map_label_to_code("Net cash from operating activities", "cash_flow") == "CF_OCF"
    # scope: a balance-sheet row must not grab an income-statement code
    assert e.map_label_to_code("Interest income", "balance_sheet") is None
    assert e.map_label_to_code("totally unknown row", "balance_sheet") is None


def test_label_to_code_only_emits_canonical_codes():
    for group, rules in e._LABEL_RULES.items():
        for code, _ in rules:
            assert code in e.KNOWN_ACCOUNT_CODES, f"{group}:{code}"


def test_detect_statement_titles():
    text = ("Statement of Financial Position\n...\nIncome Statement\n...\n"
            "Statement of Cash Flows\n...\nStatement of Changes in Equity\n")
    types = [t[0] for t in e.detect_statement_titles(text)]
    assert types[0] == "balance_sheet"                     # sorted by position
    assert set(types) == {"balance_sheet", "income_statement", "cash_flow", "changes_in_equity"}


def test_detect_statement_titles_combined_pl_oci_is_income():
    # a combined "profit or loss" page classifies as the income statement
    assert e.detect_statement_titles("Statement of profit or loss")[0][0] == "income_statement"


def test_detect_unit_scale():
    assert e.detect_unit_scale("Amounts in thousands of Qatari Riyals") == 1000
    assert e.detect_unit_scale("In QR'000") == 1000
    assert e.detect_unit_scale("All amounts in millions of QAR") == 1_000_000
    assert e.detect_unit_scale("Annual Report 2024") is None


@pytest.mark.parametrize("raw,want", [
    ("1,234", 1234), ("(56)", -56), ("12.3%", 12.3), ("QAR 1,000", 1000),
    ("—", None), ("-", None), ("n/a", None), ("nil", None), (None, None),
    (-7, -7), (3.5, 3.5), (True, None),
])
def test_coerce_number(raw, want):
    assert e._coerce_number(raw) == want


@pytest.mark.parametrize("raw,want", [
    ("Unqualified opinion", "unqualified"), ("an unmodified opinion", "unqualified"),
    ("qualified opinion", "qualified"), ("except for", "qualified"),
    ("adverse", "adverse"), ("disclaimer of opinion", "disclaimer"),
    ("review conclusion", "review"), ("", "unknown"), ("blah", "unknown"),
])
def test_coerce_opinion(raw, want):
    assert e._coerce_opinion(raw) == want


def test_audit_and_notes_hints():
    assert e._AUDIT_HINT.search("Report of the Independent Auditor")
    assert e._AUDIT_HINT.search("In our opinion, the statements ...")
    assert not e._AUDIT_HINT.search("just a normal page")
    assert e._NOTES_HINT.search("Notes to the consolidated financial statements")
    assert e._NOTES_HINT.search("See Note 14 for details")
    assert not e._NOTES_HINT.search("balance sheet figures")


def test_note_category():
    assert e._note_category("Contingent liabilities and commitments") == "contingent_liabilities"
    assert e._note_category("Sukuk financing") == "sukuk_islamic"
    assert e._note_category("Something else") == "other"


# ── guided extraction end-to-end (LLM stubbed) ───────────────────────────────

def _small_model(messages, args):
    """A stand-in for a tiny local model: only emits small flat JSON."""
    user = messages[1]["content"]
    if "EVERY line" in user:
        if "balance sheet" in user:
            return '{"rows":[{"label":"Total assets","current":"1,000","prior":"900"},' \
                   '{"label":"Total equity","current":200,"prior":180}]}'
        if "income statement" in user:
            return 'sure:\n{"rows":[{"label":"Net interest income","current":50,"prior":45},' \
                   '{"label":"Profit for the year","current":30,"prior":25}]}'
        if "cash flow" in user:
            return '{"rows":[{"label":"Net cash from operating activities","current":40,"prior":null}]}'
        return '{"rows":[]}'
    if "auditor" in user:
        return '{"opinion":"unqualified","auditor":"KPMG"}'
    if "List each note" in user:
        return '{"notes":[{"number":"27","title":"Contingent liabilities"}]}'
    return "{}"


def _pages():
    return [
        {"num": 1, "text": "Independent Auditor's Report. In our opinion ... "
                           "(Amounts in thousands of Qatari Riyals)"},
        {"num": 2, "text": "Statement of Financial Position\nTotal assets 1,000\nTotal equity 200\n"},
        {"num": 3, "text": "Income Statement\nNet interest income 50\nProfit for the year 30\n"},
        {"num": 4, "text": "Statement of Cash Flows\nNet cash from operating activities 40\n"},
        {"num": 5, "text": "Notes to the financial statements\nNote 27 Contingent liabilities ..."},
    ]


def test_guided_end_to_end_is_conforming(monkeypatch):
    monkeypatch.setattr(e, "call_llm", _small_model)
    out = e.extract_filing(_pages(), _pargs(guided=True, guided_notes=True))

    assert e.validate_filing(out) == []                     # uploadable
    codes = {li["account_code"] for s in out["statements"] for li in s["line_items"]}
    assert {"BS_TOTAL_ASSETS", "BS_TOTAL_EQUITY", "IS_NET_INTEREST",
            "IS_NET_INCOME", "CF_OCF"} <= codes
    assert out["metadata"]["unit_scale"] == 1000
    assert out["audit"]["opinion_type"] == "unqualified" and out["audit"]["auditor_name"] == "KPMG"
    assert out["notes"][0]["category"] == "contingent_liabilities"
    # comparatives recovered, prior-year label derived from fiscal_year - 1
    ta = next(li for s in out["statements"] for li in s["line_items"]
              if li["label_verbatim"] == "Total assets")
    assert ta["value"] == 1000 and ta["comparatives"] == [{"period_label": "2023", "value": 900}]


def test_guided_skips_unparseable_rows(monkeypatch):
    def flaky(messages, args):
        user = messages[1]["content"]
        if "EVERY line" in user and "balance sheet" in user:
            return "the model rambled and produced no json"
        if "EVERY line" in user and "income statement" in user:
            return '{"rows":[{"label":"Profit for the year","current":30}]}'
        return "{}"
    monkeypatch.setattr(e, "call_llm", flaky)
    pages = [
        {"num": 1, "text": "Statement of Financial Position\nTotal assets 1\n"},
        {"num": 2, "text": "Income Statement\nProfit for the year 30\n"},
    ]
    out = e.extract_filing(pages, _pargs(guided=True))
    types = {s["type"] for s in out["statements"]}
    assert types == {"income_statement"}                    # bad window dropped, good one survived
    assert e.validate_filing(out) == []


def test_guided_default_off_for_cloud(monkeypatch):
    # Without guided, the normal single-prompt path is used (one big JSON object).
    payload = json.dumps({
        "metadata": {}, "audit": {"opinion_type": "unknown", "verbatim_text": ""},
        "statements": [{"type": "balance_sheet", "verbatim_text": "BS",
                        "line_items": [{"label_verbatim": "Total assets", "value": 1,
                                        "account_code": "BS_TOTAL_ASSETS"}]}],
        "notes": [], "extraction_quality": {}})
    seen = {}
    def fake(messages, args):
        seen["system"] = messages[0]["content"]
        return payload
    monkeypatch.setattr(e, "call_llm", fake)
    out = e.extract_filing([{"num": 1, "text": "x"}], _pargs(no_chunk=True, guided=False))
    assert out["statements"][0]["type"] == "balance_sheet"
    assert "meticulous financial-filing extraction engine" in seen["system"]   # the big prompt
