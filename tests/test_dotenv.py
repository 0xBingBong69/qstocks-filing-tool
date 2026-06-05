"""Tests for the .env loader (inline-comment handling) and the provider
self-diagnostic shown by --list-providers."""
from __future__ import annotations

import pytest

import qscreen_ingest as e


_PROVIDER_ENV = ("MINIMAX_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY",
                 "ANTHROPIC_API_KEY", "MOONSHOT_API_KEY", "KIMI_API_KEY",
                 "OLLAMA_API_KEY", "LMSTUDIO_API_KEY", "LLAMACPP_API_KEY",
                 "JAN_API_KEY", "GPT4ALL_API_KEY", "MLX_API_KEY",
                 "QSCREEN_PROVIDER", "LLM_PROVIDER", "QSCREEN_MODEL", "LLM_API_KEY",
                 "QSCREEN_BASE_URL", "LLM_BASE_URL", "QSCREEN_GUIDED")


@pytest.fixture
def clean_env(monkeypatch):
    for k in _PROVIDER_ENV:
        monkeypatch.delenv(k, raising=False)


# ── _dotenv_value: inline comments / quotes ──────────────────────────────────

@pytest.mark.parametrize("raw,want", [
    ("sk-abc", "sk-abc"),
    ("sk-abc   # note", "sk-abc"),                 # the footgun: inline comment dropped
    ("  spaced  ", "spaced"),
    ('"a # b"', "a # b"),                           # quoted → '#' kept
    ("'x'", "x"),
    ("sk-a#b", "sk-a#b"),                           # '#' with no leading space is part of value
    ("# all comment", ""),                          # value that is only a comment
    ("", ""),
    ("sk-123 \t# tab-spaced note", "sk-123"),
])
def test_dotenv_value(raw, want):
    assert e._dotenv_value(raw) == want


# ── _parse_dotenv: the analyst's exact footgun + general cases ───────────────

def test_parse_dotenv_strips_inline_comment_like_template():
    text = (
        "# a comment line\n"
        "MINIMAX_API_KEY=sk-kimi-REDACTED          # minimax  get a key: https://platform.minimax.io/\n"
        "MOONSHOT_API_KEY=sk-moon-xyz   # kimi\n"
        "export OPENAI_API_KEY=sk-oai\n"
        "EMPTY=\n"
        'QUOTED="v # not-a-comment"\n'
    )
    env = e._parse_dotenv(text)
    assert env["MINIMAX_API_KEY"] == "sk-kimi-REDACTED"      # comment + trailing spaces gone
    assert env["MOONSHOT_API_KEY"] == "sk-moon-xyz"
    assert env["OPENAI_API_KEY"] == "sk-oai"                 # `export ` prefix handled
    assert env["EMPTY"] == ""
    assert env["QUOTED"] == "v # not-a-comment"


# ── provider_diagnostic ──────────────────────────────────────────────────────

def test_diagnostic_none_when_nothing_set(clean_env):
    assert e.provider_diagnostic().startswith("✗ No provider detected")


def test_diagnostic_kimi_key_in_moonshot(clean_env, monkeypatch):
    monkeypatch.setenv("MOONSHOT_API_KEY", "sk-moon")
    out = e.provider_diagnostic()
    assert out.startswith("✓ Detected provider: kimi") and "MOONSHOT_API_KEY" in out


def test_diagnostic_local_runtime(clean_env, monkeypatch):
    monkeypatch.setenv("QSCREEN_PROVIDER", "ollama")
    assert e.provider_diagnostic() == "✓ Detected local runtime: ollama (no API key needed)."


def test_diagnostic_selected_but_no_key(clean_env, monkeypatch):
    monkeypatch.setenv("QSCREEN_PROVIDER", "openai")
    out = e.provider_diagnostic()
    assert out.startswith("⚠ Provider 'openai' is selected") and "OPENAI_API_KEY" in out


def test_diagnostic_surfaces_wrong_variable(clean_env, monkeypatch):
    # The actual bug report: a Kimi key left in MINIMAX_API_KEY is detected as
    # 'minimax' — so the diagnostic shows the mismatch instead of silent failure.
    monkeypatch.setenv("MINIMAX_API_KEY", "sk-kimi-REDACTED")
    out = e.provider_diagnostic()
    assert "minimax" in out and "kimi" not in out
