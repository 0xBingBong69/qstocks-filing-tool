#!/usr/bin/env python3
"""
qscreen_ingest.py — ONE-FILE QSE filing ingestor for qscreen.app.

Self-contained: schema + extractor + uploader in a single file (no imports of
sibling modules, no multi-file setup). Give it a PDF and it produces a
qscreen-uploadable JSON and (unless --dry-run) POSTs it to the site.

USAGE (the only command an operator/agent needs):
    python3 qscreen_ingest.py <PDF> --symbol QIBK --sector islamic_bank --year 2024 --period FY

  sectors: conventional_bank | islamic_bank | industrial | insurance | other
  periods: FY | Q1 | Q2 | Q3 | Q4 | H1 | 9M   (default FY)

CONFIG (put in a file named `.env` next to this script, or real env vars):
    MINIMAX_API_KEY=...                 # LLM key for your provider; the tool
                                        # auto-detects minimax / openrouter /
                                        # kimi / openai / anthropic from whichever
                                        # *_API_KEY is set (see --list-providers)
    INGEST_TOKEN=...                    # qscreen.app ingest token (needed to upload)
    QSCREEN_API_URL=https://qscreen.app # defaults to http://localhost:3004

  No key, fully offline?  Run a model on your laptop and force its provider:
    python3 qscreen_ingest.py <PDF> --provider mlx --basic --symbol QIBK ... --dry-run
  Local runtimes (ollama / mlx / lmstudio / llamacpp / jan / gpt4all) need NO key.
  --basic (auto-on for local) reads the numbers from the PDF's tables in code, so
  even a 270M Gemma works; --no-llm skips the model entirely.

DEPS:  pip install pdfplumber requests

The tool: PDF -> (pdfplumber text + recovered tables) -> page windows -> EITHER
Basic (numbers parsed from the recovered tables in code; the model only fills gaps
+ classifies the audit opinion — great for tiny/local models, or --no-llm for none)
OR Pro (one big LLM prompt extracts everything; use a strong model) -> normalized +
merged lossless JSON -> validated against the contract -> saved AND uploaded.
Self-test: --self-test.   Providers/modes: --list-providers.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

__version__ = "1.0.0"


# ── .env loader (no python-dotenv dependency) ────────────────────────────────

def _dotenv_value(val: str) -> str:
    """Parse one .env value: honour a surrounding quote, else drop an inline
    comment. So `KEY=abc   # note` → 'abc', `KEY="a # b"` → 'a # b', and a value
    that is only a comment → ''. (The shipped .env.example puts a ' # ...' note
    after each key, so this stops that note being captured as part of the key.)"""
    v = val.strip()
    if v[:1] in ('"', "'"):                       # quoted → take what's inside the quotes
        end = v.find(v[0], 1)
        return v[1:end] if end != -1 else v[1:]
    for i, ch in enumerate(v):                    # unquoted → cut at a '#' comment
        if ch == "#" and (i == 0 or v[i - 1] in " \t"):
            return v[:i].rstrip()
    return v.rstrip()


def _parse_dotenv(text: str) -> dict:
    """Parse .env text → {key: value}. Tolerates blank/comment lines, an optional
    `export ` prefix, and inline comments (see _dotenv_value)."""
    out: dict = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        if key:
            out[key] = _dotenv_value(val)
    return out


def _load_dotenv() -> None:
    here = Path(__file__).resolve().parent
    for candidate in (here / ".env", here.parent / ".env"):
        if not candidate.is_file():
            continue
        for key, val in _parse_dotenv(candidate.read_text(encoding="utf-8")).items():
            if key not in os.environ:             # real env vars win over .env
                os.environ[key] = val


_load_dotenv()

# Qatar per-stock knowledge base (optional import — the engine still runs without
# it; when present it makes extraction company- and year-aware).
try:
    import qatar
except Exception:  # pragma: no cover - defensive
    qatar = None


# ════════════════════════════════════════════════════════════════════════════
#  CONTRACT  (the lossless filing schema — inlined so this file stands alone)
# ════════════════════════════════════════════════════════════════════════════

KNOWN_ACCOUNT_CODES = {
    "IS_REVENUE", "IS_NET_INTEREST", "IS_INTEREST_INCOME", "IS_INTEREST_EXP",
    "IS_FEES_COMM", "IS_FX_GAIN", "IS_INVESTMENT_INCOME", "IS_OTHER_INCOME",
    "IS_GROSS_PREMIUMS", "IS_NET_PREMIUMS", "IS_CLAIMS", "IS_NET_ECL",
    "IS_OTHER_PROVISIONS", "IS_STAFF", "IS_OPERATING_EXP", "IS_DEPRECIATION",
    "IS_AMORT_INTANGIBLE", "IS_SHARE_ASSOCIATES", "IS_OPERATING_PROFIT",
    "IS_PROFIT_BEFORE_TAX", "IS_INCOME_TAX", "IS_NET_MONETARY", "IS_NCI",
    "IS_NET_INCOME", "IS_EPS",
    "BS_CASH", "BS_TREASURY", "BS_DUE_FROM_BANKS", "BS_TRADING_INVEST",
    "BS_FVTPL", "BS_FVOCI", "BS_LOANS", "BS_SUKUK", "BS_AT1", "BS_TOTAL_ASSETS",
    "BS_DUE_TO_BANKS", "BS_CUSTOMER_DEPOSITS", "BS_TOTAL_LIABILITIES",
    "BS_SHARE_CAPITAL", "BS_RETAINED", "BS_TOTAL_EQUITY", "BS_TLOE",
    "CF_OCF", "CF_ICF", "CF_FCF", "CF_CAPEX", "CF_DIVIDENDS_PAID", "CF_NET_CHANGE",
    "KPI_NIM", "KPI_NPL", "KPI_CAR", "KPI_LDR", "KPI_COST_INCOME",
    "KPI_COVERAGE", "KPI_ROE", "KPI_ROA", "KPI_GWP", "KPI_NET_PREMIUMS",
    "KPI_LOSS_RATIO", "KPI_EXPENSE_RATIO", "KPI_COMBINED",
}

STATEMENT_TYPES = {
    "income_statement", "balance_sheet", "cash_flow",
    "changes_in_equity", "comprehensive_income",
}

AUDIT_OPINION_TYPES = {
    "unqualified", "qualified", "adverse", "disclaimer", "review", "unknown",
}

NOTE_CATEGORIES = {
    "accounting_policies", "critical_estimates", "segment_information",
    "cost_breakdown", "other_income", "other_comprehensive_income",
    "contingent_liabilities", "commitments", "related_party",
    "subsequent_events", "going_concern", "fair_value",
    "financial_instruments_risk", "capital_adequacy", "ecl_provisions",
    "sukuk_islamic", "insurance_technical", "other",
}

FISCAL_PERIODS = {"FY", "Q1", "Q2", "Q3", "Q4", "H1", "9M"}
SECTORS = ["conventional_bank", "islamic_bank", "industrial", "insurance", "other"]
SEGMENT_DIMENSIONS = {"business_line", "geography", "legal_entity"}


def empty_filing() -> dict:
    return {
        "metadata": {
            "symbol": None, "company_name": None, "sector": None,
            "fiscal_year": None, "fiscal_period": None, "period_end": None,
            "currency": "QAR", "unit_scale": 1, "reporting_framework": None,
            "consolidated": None, "language": None, "source_file": None,
            "source_sha256": None, "extracted_at": None,
            "extractor": {"provider": None, "model": None},
        },
        "audit": {
            "opinion_type": "unknown", "auditor_name": None, "report_date": None,
            "emphasis_of_matter": [], "key_audit_matters": [],
            "material_uncertainty_going_concern": {"present": False, "text": ""},
            "verbatim_text": "",
        },
        "statements": [],
        "segments": [],
        "notes": [],
        "extraction_quality": {"confidence": None, "warnings": [], "unmapped_labels": []},
    }


def validate_filing(data: dict) -> list[str]:
    problems: list[str] = []
    if not isinstance(data, dict):
        return ["top-level value is not an object"]

    meta = data.get("metadata")
    if not isinstance(meta, dict):
        problems.append("metadata: missing or not an object")
    else:
        if meta.get("fiscal_period") not in (None, *FISCAL_PERIODS):
            problems.append(f"metadata.fiscal_period: invalid value {meta.get('fiscal_period')!r}")
        if meta.get("unit_scale") not in (1, 1000, 1000000, None):
            problems.append(f"metadata.unit_scale: must be 1/1000/1000000, got {meta.get('unit_scale')!r}")

    audit = data.get("audit")
    if not isinstance(audit, dict):
        problems.append("audit: missing or not an object")
    else:
        if audit.get("opinion_type") not in AUDIT_OPINION_TYPES:
            problems.append(f"audit.opinion_type: invalid {audit.get('opinion_type')!r}")
        if audit.get("opinion_type") not in (None, "unknown") and not audit.get("verbatim_text"):
            problems.append("audit.verbatim_text: opinion present but verbatim text empty (lossy)")
        for k, kam in enumerate(audit.get("key_audit_matters") or []):
            if not isinstance(kam, dict) or not kam.get("title") or not kam.get("text"):
                problems.append(f"audit.key_audit_matters[{k}]: must have non-empty title and text")

    statements = data.get("statements")
    if not isinstance(statements, list):
        problems.append("statements: missing or not a list")
    elif not statements:
        problems.append("statements: empty — extraction likely failed (no core statements captured)")
    else:
        for i, st in enumerate(statements):
            if st.get("type") not in STATEMENT_TYPES:
                problems.append(f"statements[{i}].type: invalid {st.get('type')!r}")
            if not st.get("verbatim_text"):
                problems.append(f"statements[{i}].verbatim_text: empty (lossy)")
            for j, li in enumerate(st.get("line_items", [])):
                code = li.get("account_code")
                if code is not None and code not in KNOWN_ACCOUNT_CODES:
                    problems.append(f"statements[{i}].line_items[{j}].account_code: unknown {code!r}")
                if not li.get("label_verbatim"):
                    problems.append(f"statements[{i}].line_items[{j}].label_verbatim: empty (lossy)")
                comps = li.get("comparatives")
                if comps is not None:
                    if not isinstance(comps, list):
                        problems.append(f"statements[{i}].line_items[{j}].comparatives: must be a list")
                    else:
                        for c, comp in enumerate(comps):
                            # Require BOTH a period_label and a value — a comparative
                            # with a label but no value is silently lost downstream.
                            if not isinstance(comp, dict) or not comp.get("period_label") \
                                    or comp.get("value") is None:
                                problems.append(
                                    f"statements[{i}].line_items[{j}].comparatives[{c}]: "
                                    "need both period_label and value")

    # segments[] is an optional, additive section (absent or [] is fine).
    segments = data.get("segments")
    if segments is not None and not isinstance(segments, list):
        problems.append("segments: must be a list")
    elif isinstance(segments, list):
        for i, sg in enumerate(segments):
            if not isinstance(sg, dict):
                problems.append(f"segments[{i}]: not an object")
                continue
            if sg.get("dimension") not in SEGMENT_DIMENSIONS:
                problems.append(f"segments[{i}].dimension: invalid {sg.get('dimension')!r}")
            if not sg.get("name"):
                problems.append(f"segments[{i}].name: empty")
            if sg.get("metrics") is not None and not isinstance(sg.get("metrics"), dict):
                problems.append(f"segments[{i}].metrics: must be an object")

    notes = data.get("notes")
    if not isinstance(notes, list):
        problems.append("notes: missing or not a list")
    else:
        for i, nt in enumerate(notes):
            if nt.get("category") not in NOTE_CATEGORIES:
                problems.append(f"notes[{i}].category: invalid {nt.get('category')!r}")
            if not nt.get("verbatim_text"):
                problems.append(f"notes[{i}].verbatim_text: empty (lossy)")

    return problems


# ════════════════════════════════════════════════════════════════════════════
#  PROVIDERS
# ════════════════════════════════════════════════════════════════════════════

# Each provider: the API base URL, the wire protocol ("openai" = the OpenAI
# chat/completions shape, "anthropic" = the Anthropic Messages shape), the
# env var(s) its key lives in, and a sensible default model. Add a provider by
# adding a row here — nothing else needs to change. Override any field per-run
# with --base-url / --model / --llm-key (or env QSCREEN_MODEL).
#
# `local: True` marks a runtime you download and run on your own machine
# (Ollama, LM Studio, llama.cpp, …). These expose the OpenAI chat shape on
# localhost and need NO API key. `key_url` for a local provider is the
# install/download page, and `setup` is a one-line "how to run it" hint.
PROVIDERS = {
    "minimax":    {"label": "MiniMax", "base_url": "https://api.minimax.io/v1", "kind": "openai",
                   "env": ("MINIMAX_API_KEY",), "default_model": "MiniMax-M2",
                   "key_url": "https://platform.minimax.io/"},
    "openrouter": {"label": "OpenRouter", "base_url": "https://openrouter.ai/api/v1", "kind": "openai",
                   "env": ("OPENROUTER_API_KEY",), "default_model": "minimax/minimax-01",
                   "key_url": "https://openrouter.ai/keys"},
    "kimi":       {"label": "Kimi (Moonshot)", "base_url": "https://api.moonshot.ai/v1", "kind": "openai",
                   "env": ("MOONSHOT_API_KEY", "KIMI_API_KEY"), "default_model": "kimi-k2-0905-preview",
                   "key_url": "https://platform.moonshot.ai/console/api-keys"},
    "openai":     {"label": "OpenAI", "base_url": "https://api.openai.com/v1", "kind": "openai",
                   "env": ("OPENAI_API_KEY",), "default_model": "gpt-4o",
                   "key_url": "https://platform.openai.com/api-keys"},
    "anthropic":  {"label": "Claude (Anthropic)", "base_url": "https://api.anthropic.com/v1", "kind": "anthropic",
                   "env": ("ANTHROPIC_API_KEY",), "default_model": "claude-sonnet-4-5",
                   "key_url": "https://console.anthropic.com/settings/keys"},
    # ── Local / offline runtimes (no API key; run a model on your laptop) ──
    # `no_system`: chat template has no system role (fold it into the user turn).
    # `schema_style`: how this server accepts a JSON schema for the small Basic asks
    # ("ollama_format" = native `format`; "openai_json_schema" = response_format; None = neither).
    "ollama":     {"label": "Ollama (local)", "base_url": "http://localhost:11434/v1", "kind": "openai",
                   "env": ("OLLAMA_API_KEY",), "default_model": "gemma2:2b", "local": True,
                   "schema_style": "ollama_format", "key_url": "https://ollama.com/download",
                   "setup": "install Ollama, then:  ollama pull gemma2:2b"},
    "lmstudio":   {"label": "LM Studio (local)", "base_url": "http://localhost:1234/v1", "kind": "openai",
                   "env": ("LMSTUDIO_API_KEY",), "default_model": "local-model", "local": True,
                   "schema_style": "openai_json_schema", "key_url": "https://lmstudio.ai/",
                   "setup": "open LM Studio → load a model → Developer → Start Server"},
    "llamacpp":   {"label": "llama.cpp (local)", "base_url": "http://localhost:8080/v1", "kind": "openai",
                   "env": ("LLAMACPP_API_KEY",), "default_model": "local-model", "local": True,
                   "key_url": "https://github.com/ggml-org/llama.cpp",
                   "setup": "run:  llama-server -m your-model.gguf"},
    "jan":        {"label": "Jan (local)", "base_url": "http://localhost:1337/v1", "kind": "openai",
                   "env": ("JAN_API_KEY",), "default_model": "local-model", "local": True,
                   "key_url": "https://jan.ai/",
                   "setup": "Jan → Settings → Local API Server → Start"},
    "gpt4all":    {"label": "GPT4All (local)", "base_url": "http://localhost:4891/v1", "kind": "openai",
                   "env": ("GPT4ALL_API_KEY",), "default_model": "local-model", "local": True,
                   "key_url": "https://www.nomic.ai/gpt4all",
                   "setup": "GPT4All → Settings → enable the local API server"},
    "mlx":        {"label": "MLX (Apple, local)", "base_url": "http://localhost:8080/v1", "kind": "openai",
                   "env": ("MLX_API_KEY",), "default_model": "mlx-community/gemma-3-270m-it-4bit",
                   "local": True, "no_system": True,
                   "key_url": "https://github.com/ml-explore/mlx-lm",
                   "setup": "pip install mlx-lm; mlx_lm.server --model mlx-community/gemma-3-270m-it-4bit"},
}
# Friendly aliases the user can type for --provider / QSCREEN_PROVIDER.
PROVIDER_ALIASES = {"claude": "anthropic", "moonshot": "kimi", "gpt": "openai", "oai": "openai",
                    "llama.cpp": "llamacpp", "llama-cpp": "llamacpp", "lm-studio": "lmstudio",
                    "lm_studio": "lmstudio", "local": "ollama", "apple": "mlx", "mlx-lm": "mlx"}


def is_local_provider(name: str | None) -> bool:
    """True for a runtime that runs on the user's own machine (no API key)."""
    cfg = PROVIDERS.get(canonical_provider(name) or "")
    return bool(cfg and cfg.get("local"))
# Provider names accepted on the CLI (plus "custom" for any OpenAI-compatible URL).
PROVIDER_CHOICES = sorted(set(PROVIDERS) | set(PROVIDER_ALIASES) | {"custom"})


def canonical_provider(name: str | None) -> str | None:
    if not name:
        return None
    n = name.strip().lower()
    return PROVIDER_ALIASES.get(n, n)


def default_model(name: str | None) -> str | None:
    cfg = PROVIDERS.get(canonical_provider(name) or "")
    return cfg["default_model"] if cfg else None


def list_providers() -> str:
    cloud = {n: p for n, p in PROVIDERS.items() if not p.get("local")}
    local = {n: p for n, p in PROVIDERS.items() if p.get("local")}
    rows = ["Cloud providers — put the matching API key in .env (the tool auto-detects it):", ""]
    for name, p in cloud.items():
        rows.append(f"  {name:11s} {p['label']:18s} {p['env'][0]:20s} model={p['default_model']}")
        rows.append(f"  {'':11s} └─ get a key:  {p['key_url']}")
    rows.append(f"  {'custom':11s} {'(any OpenAI URL)':18s} {'LLM_API_KEY':20s} pass --base-url --model")
    rows.append("")
    rows.append("Local runtimes — run a model on your own laptop, NO API key needed:")
    rows.append("")
    for name, p in local.items():
        rows.append(f"  {name:11s} {p['label']:18s} {p['base_url']:28s} model={p['default_model']}")
        rows.append(f"  {'':11s} └─ {p.get('setup', 'download: ' + p['key_url'])}")
    rows.append("")
    rows.append("Aliases: " + ", ".join(f"{a}→{b}" for a, b in PROVIDER_ALIASES.items()))
    rows.append("Force one with --provider NAME (or env QSCREEN_PROVIDER); pick a model with "
                "--model (or env QSCREEN_MODEL).")
    rows.append("")
    rows.append("Modes:  --basic = deterministic-first (reads numbers from the PDF's tables; "
                "great for tiny/local models, auto-on for local runtimes)")
    rows.append("        --pro   = the model extracts everything (use a strong model: "
                "GPT-4.5+/Claude Sonnet 4+/MiniMax-M2)")
    rows.append("        --no-llm = Basic with NO model at all (fully offline; needs no key)")
    return "\n".join(rows)


def detect_provider() -> str | None:
    """Provider from QSCREEN_PROVIDER/LLM_PROVIDER, else the first one whose key is set."""
    env_choice = canonical_provider(os.getenv("QSCREEN_PROVIDER") or os.getenv("LLM_PROVIDER"))
    if env_choice:
        return env_choice
    for name, p in PROVIDERS.items():
        if any(os.getenv(k) for k in p["env"]):
            return name
    return None


def provider_diagnostic() -> str:
    """One human-readable line saying what the tool detects right now from the
    environment / .env — so `--list-providers` can self-diagnose a missing or
    misplaced key (e.g. a Kimi key left in MINIMAX_API_KEY)."""
    name = detect_provider()
    if not name:
        return ("✗ No provider detected. Put one *_API_KEY in .env (the key alone after '=', "
                "an inline '# ...' note is fine), or set QSCREEN_PROVIDER for a local runtime.")
    if is_local_provider(name):
        return f"✓ Detected local runtime: {name} (no API key needed)."
    cfg = PROVIDERS.get(name) or {}
    if any(os.getenv(k) for k in (cfg.get("env") or ())) or os.getenv("LLM_API_KEY"):
        env_name = next((k for k in (cfg.get("env") or ()) if os.getenv(k)), "LLM_API_KEY")
        return f"✓ Detected provider: {name} (API key found in {env_name})."
    want = (cfg.get("env") or ["<KEY>"])[0]
    return (f"⚠ Provider '{name}' is selected (QSCREEN_PROVIDER) but no API key is set for it — "
            f"add {want}=... to .env.")


def resolve_provider(args) -> dict:
    """Turn the chosen provider + overrides + env into a concrete
    {name, base_url, kind, model, key}. Raises SystemExit with guidance if the
    provider or key can't be determined — no network is touched."""
    name = canonical_provider(getattr(args, "provider", None)) or detect_provider()
    if not name:
        raise SystemExit("No LLM provider selected and no provider API key found.\n\n" + list_providers())

    if name == "custom":
        base = (getattr(args, "base_url", None) or os.getenv("QSCREEN_BASE_URL")
                or os.getenv("LLM_BASE_URL"))
        if not base:
            raise SystemExit("--provider custom requires --base-url (any OpenAI-compatible endpoint).")
        cfg = {"base_url": base, "kind": "openai", "env": ("LLM_API_KEY",), "default_model": None}
    else:
        cfg = PROVIDERS.get(name)
        if not cfg:
            raise SystemExit(f"Unknown provider {name!r}.\n\n" + list_providers())
        cfg = dict(cfg)
        # Let a local runtime live on a different host/port without --base-url.
        override = (getattr(args, "base_url", None) or os.getenv("QSCREEN_BASE_URL")
                    or (os.getenv("LLM_BASE_URL") if cfg.get("local") else None))
        if override:
            cfg["base_url"] = override

    # `local` (a named on-laptop runtime) drives the guided default; a custom
    # endpoint is often local too, so its key is OPTIONAL — but we don't presume
    # it's small, so it doesn't auto-enable guided.
    local = bool(cfg.get("local"))
    key_optional = local or name == "custom"
    model = getattr(args, "model", None) or os.getenv("QSCREEN_MODEL") or cfg["default_model"]
    if not model:
        raise SystemExit(f"No model for provider {name!r}; pass --model or set QSCREEN_MODEL.")
    key = (getattr(args, "llm_key", None)
           or next((os.getenv(k) for k in cfg["env"] if os.getenv(k)), None)
           or os.getenv("LLM_API_KEY"))
    if not key:
        if key_optional:
            key = "local"   # local servers ignore the bearer token; send a placeholder
        else:
            want = " or ".join(cfg["env"]) + " (or LLM_API_KEY)"
            raise SystemExit(f"No API key for provider {name!r}. Set {want} in .env or pass --llm-key.")
    return {"name": name, "base_url": cfg["base_url"].rstrip("/"),
            "kind": cfg["kind"], "model": model, "key": key, "local": local,
            "no_system": bool(cfg.get("no_system")), "schema_style": cfg.get("schema_style")}


def deterministic_cfg() -> dict:
    """A metadata-only provider stub for fully-offline (--no-llm) runs, so the
    extractor never has to resolve a real provider or touch the network."""
    return {"name": "deterministic", "base_url": "", "kind": "openai",
            "model": "none", "key": "local", "local": True,
            "no_system": False, "schema_style": None}


# ── PDF → pages (text + recovered tables, optional OCR) ──────────────────────

OCR_MIN_CHARS = 20   # a page with fewer than this many non-space chars is "empty"
OCR_DPI = 300


class OcrUnavailable(RuntimeError):
    """Raised when OCR is requested but pytesseract/pdf2image aren't installed."""


def _ocr_pages(pdf_path: str, page_numbers: list[int]) -> dict[int, str]:
    """OCR specific 1-based page numbers; returns {page_num: text}."""
    try:
        import pytesseract
        from pdf2image import convert_from_path
    except Exception as e:  # missing python deps OR missing system tesseract/poppler
        raise OcrUnavailable(str(e))
    out: dict[int, str] = {}
    for n in page_numbers:
        try:
            images = convert_from_path(pdf_path, first_page=n, last_page=n, dpi=OCR_DPI)
        except Exception:
            continue
        if images:
            out[n] = pytesseract.image_to_string(images[0]) or ""
    return out


def pdf_to_pages(pdf_path: str, ocr_mode: str = "auto") -> tuple[list[dict], str]:
    """Extract per-page text (+ recovered tables). ocr_mode: auto|never|always.

    auto   — OCR only pages that pdfplumber returned (almost) no text for.
    always — OCR every page (slow; for fully-scanned filings).
    never  — text layer only.
    """
    import pdfplumber
    raw = Path(pdf_path).read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    pages: list[dict] = []
    with pdfplumber.open(pdf_path) as pdf:
        for page_num, page in enumerate(pdf.pages, 1):
            text = page.extract_text() or ""
            rendered = _render_tables(page)
            if rendered:
                text = f"{text}\n\n[TABLES on page {page_num}]\n{rendered}"
            pages.append({"num": page_num, "text": text})

    if ocr_mode == "always":
        targets = [p["num"] for p in pages]
    elif ocr_mode == "auto":
        targets = [p["num"] for p in pages if len(p["text"].strip()) < OCR_MIN_CHARS]
    else:
        targets = []

    if targets:
        try:
            ocr_map = _ocr_pages(pdf_path, targets)
            recovered = 0
            for p in pages:
                add = (ocr_map.get(p["num"]) or "").strip()
                if add:
                    p["text"] = (f"{p['text']}\n\n[OCR text]\n{add}"
                                 if p["text"].strip() else add)
                    recovered += 1
            if recovered:
                print(f"   🔎 OCR recovered text from {recovered} page(s)")
        except OcrUnavailable:
            scanned = len([p for p in pages if len(p["text"].strip()) < OCR_MIN_CHARS])
            msg = ("OCR is not available (install: pip install pytesseract pdf2image, "
                   "plus system 'tesseract' and 'poppler')")
            if ocr_mode == "always":
                raise SystemExit(f"--ocr always requested but {msg}.")
            print(f"   ⚠️  {scanned} page(s) have little/no extractable text (likely "
                  f"scanned). {msg}; re-run with --ocr always to force.")
    return pages, sha


def _render_tables(page) -> str:
    out_lines: list[str] = []
    try:
        tables = page.extract_tables() or []
    except Exception:
        return ""
    for ti, table in enumerate(tables, 1):
        if not table or len(table) < 2:
            continue
        out_lines.append(f"-- table {ti} --")
        for row in table:
            cells = [("" if c is None else str(c).replace("\n", " ").strip()) for c in row]
            if any(cells):
                out_lines.append(" | ".join(cells))
    return "\n".join(out_lines)


def page_windows(pages: list[dict], size: int, overlap: int) -> list[list[dict]]:
    if size <= 0:
        return [pages]
    step = max(1, size - overlap)
    windows, i = [], 0
    while i < len(pages):
        windows.append(pages[i:i + size])
        if i + size >= len(pages):
            break
        i += step
    return windows


def render_window(window: list[dict]) -> str:
    return "".join(f"\n===== PAGE {p['num']} =====\n{p['text']}" for p in window)


# ── Prompt ───────────────────────────────────────────────────────────────────

def _qatar_context(pf: dict | None) -> str:
    """Render a company-and-year specific context block from a resolved profile
    (the output of qatar.profile_for_year). Empty string when no profile."""
    if not pf:
        return ""
    seg = pf.get("segments_expected") or {}
    geos = ", ".join(seg.get("by_geography") or []) or "—"
    biz = ", ".join(seg.get("by_business") or []) or "—"
    subs = "; ".join(f"{s['name']} ({s.get('country', '?')}/{s.get('currency', '?')})"
                     for s in pf.get("active_subsidiaries") or []) or "none recorded"
    evs = "; ".join(f"{e.get('year', '?')} {e.get('title', '')} — {e.get('effect', '')}"
                    for e in pf.get("active_events") or []) or "none recorded"
    kpis = ", ".join(pf.get("watch_kpis") or []) or "—"
    quirks = "; ".join(pf.get("accounting_quirks") or []) or "—"
    yr = pf.get("as_of_year")
    return f"""

QATAR ANALYST CONTEXT — pre-loaded knowledge about THIS specific company as of \
fiscal year {yr}. Use it to know what to look for. If the filing differs from it \
(a new acquisition, a disposal, a rename, or a regime change), capture what the \
filing ACTUALLY shows and add a short note to extraction_quality.warnings.
  Company (as of {yr}): {pf.get('name_as_of')} [{pf.get('ticker')}], \
{pf.get('sub_sector')}; reports in {pf.get('reporting_currency')} under \
{pf.get('framework_as_of')}.
  Expected business segments: {biz}
  Expected geographic segments: {geos}
  Active foreign subsidiaries & currencies by {yr}: {subs}
  Regulatory / accounting regime in force by {yr}: {evs}
  KPIs to capture if the filing prints them: {kpis}
  Known accounting quirks: {quirks}"""


def _system_prompt(sector: str, windowed: bool, profile: dict | None = None) -> str:
    codes = ", ".join(sorted(KNOWN_ACCOUNT_CODES))
    statement_types = ", ".join(sorted(STATEMENT_TYPES))
    note_cats = ", ".join(sorted(NOTE_CATEGORIES))
    opinions = ", ".join(sorted(AUDIT_OPINION_TYPES))
    scope = (
        "You are given PART of a filing (a page range). Extract every statement "
        "and every note that APPEARS in these pages. Arrays may be partial — only "
        "include what is present here; another pass covers the rest. If the "
        "independent auditor's report appears in this range, fill `audit`, else "
        "leave audit.opinion_type = \"unknown\"."
        if windowed else "Extract the COMPLETE filing in one object."
    )
    return f"""You are a meticulous financial-filing extraction engine for Qatar Stock \
Exchange (QSE) companies. You convert filing text into a single JSON object. \
You never invent numbers and never drop content. {scope}

SECTOR CONTEXT: this filing is a {sector}. Use sector-appropriate line items: \
islamic_bank reports sukuk / profit-sharing / quasi-equity and has NO interest \
income; conventional_bank reports interest income/expense and NIM; insurance \
reports gross/net premiums, claims, loss & combined ratios; industrial reports \
revenue / cost of sales / inventory. For "other" (real estate, utilities, \
telecom, transport, holding companies, services), DO NOT force a COGS/inventory \
structure — capture whatever revenue and cost lines the statement actually \
prints (e.g. rental income, occupancy, ARPU, freight/charter revenue, share of \
results of associates).{_qatar_context(profile)}

OUTPUT CONTRACT — emit ONE JSON object with EXACTLY these top-level keys:
  metadata, audit, statements, segments, notes, extraction_quality
Use these EXACT field names (do not rename):
  metadata: {{symbol, company_name, sector, fiscal_year, fiscal_period, \
period_end, currency, unit_scale, reporting_framework, consolidated, language}}
  audit: {{opinion_type, auditor_name, report_date, emphasis_of_matter (array \
of strings), key_audit_matters (array of {{title, text}}), \
material_uncertainty_going_concern ({{present, text}} or null), verbatim_text}}
  statements[]: {{type, title, period_label, verbatim_text, line_items[]}}
  line_items[]: {{account_code, label_verbatim, value, comparatives (array of \
{{period_label, value}}), note_ref, depth, is_subtotal}}
  segments[]: {{dimension ("business_line"|"geography"|"legal_entity"), name, \
currency, period_label, metrics ({{revenue, profit_before_tax, net_profit, \
total_assets, ...}}), comparatives (array of {{period_label, metrics}}), note_ref, \
verbatim_text}}
  notes[]: {{number, title, category, structured, verbatim_text}}

RULES (in priority order):
1. LOSSLESS VERBATIM. For every statement and every note, copy the COMPLETE \
original text into `verbatim_text`. Never summarize, truncate, or paraphrase. \
This is the most important rule.
2. STRUCTURED TOO. For each printed statement row emit a line_item. `value` is \
the CURRENT-period number exactly as printed (do NOT rescale); put the multiplier \
in metadata.unit_scale (1, 1000, or 1000000) from the "in thousands/millions" \
header. Use negative values for amounts printed in brackets.
   - COMPARATIVES: QSE statements print the prior period beside the current one. \
Capture each prior figure in `comparatives` as [{{"period_label": "2022", \
"value": 123}}] (newest prior first; same sign and scale as `value`). Omit or use \
[] only when the row genuinely prints no comparative.
   - account_code MUST be one of these canonical codes, or null if no clean \
match (when null, still keep label_verbatim): {codes}
   - KPI RATIOS: for any KPI_* ratio code (e.g. KPI_CAR, KPI_NPL, KPI_COST_INCOME, \
KPI_NIM, KPI_LDR, KPI_ROE, KPI_ROA, KPI_LOSS_RATIO, KPI_EXPENSE_RATIO, \
KPI_COMBINED) record `value` as the PERCENTAGE NUMBER exactly as printed — e.g. \
1.3 for "1.3%", 19.5 for "19.5%" — never as a fraction like 0.013.
   - statements[].type MUST be one of: {statement_types}
3. AUDIT. audit.opinion_type MUST be one of: {opinions}. Put the full opinion \
wording in audit.verbatim_text (NOT a field called opinion_text). Each key \
audit matter is {{title, text}} — no other keys.
4. NOTES. Every note -> one entry with number, title, category (one of: \
{note_cats}), a category-specific `structured` object, and COMPLETE \
verbatim_text. Watch for: contingent liabilities, commitments, other \
comprehensive income, cost/expense breakdowns, related-party, ECL/staged \
provisions, and Islamic profit-sharing/sukuk/quasi-equity.
4b. SEGMENTS. If the filing discloses operating-segment, business-line, or \
geographic/country breakdowns (usually a "segment information" note), ALSO emit \
them as typed `segments[]` rows — one per segment per dimension — putting each \
segment's revenue / profit / assets in `metrics` and its prior-year figures in \
`comparatives`. Set `currency` when a segment reports in a foreign currency. The \
QATAR ANALYST CONTEXT lists the segments to expect for this company; capture what \
the filing ACTUALLY shows. Keep the full note text in notes[] as well (lossless).
5. HONESTY. If a value is illegible or absent, use null and add a string to \
extraction_quality.warnings. Set extraction_quality.confidence in [0,1].

Return ONLY the JSON object, no prose, no markdown fences."""


def build_messages(filing_text: str, args, windowed: bool, page_hint: str = "") -> list[dict]:
    user = f"""Extract this QSE filing{(' segment ' + page_hint) if page_hint else ''}.

Known metadata (trust these over anything parsed):
  symbol: {args.symbol}
  sector: {args.sector}
  fiscal_year: {args.year}
  fiscal_period: {args.period}

FILING TEXT (page-delimited):
{filing_text}"""
    return [
        {"role": "system",
         "content": _system_prompt(args.sector, windowed, getattr(args, "_profile", None))},
        {"role": "user", "content": user},
    ]


# ── LLM call ──────────────────────────────────────────────────────────────────

def _merge_system_into_user(messages: list[dict]) -> list[dict]:
    """Fold all system-role content into the first user message — for chat
    templates that have NO system role (e.g. Gemma). Returns a NEW list; the
    original is untouched. If there is no user turn, the system text becomes one."""
    sys_text = "\n\n".join(m["content"] for m in messages if m.get("role") == "system")
    rest = [m for m in messages if m.get("role") != "system"]
    if not sys_text:
        return list(messages)
    out, injected = [], False
    for m in rest:
        if not injected and m.get("role") == "user":
            out.append({"role": "user", "content": f"{sys_text}\n\n{m['content']}"})
            injected = True
        else:
            out.append(dict(m))
    if not injected:
        out.insert(0, {"role": "user", "content": sys_text})
    return out


def _attach_schema(payload: dict, cfg: dict, args) -> None:
    """Best-effort structured-output enforcement for the small Basic asks. Uses an
    ephemeral `args._schema` (a JSON schema) and the provider's `schema_style`:
    Ollama takes a native `format`; LM Studio takes response_format json_schema.
    Other servers (MLX, llama.cpp) get nothing — we rely on robust JSON parsing."""
    schema = getattr(args, "_schema", None)
    style = cfg.get("schema_style")
    if not schema or not style:
        return
    if style == "ollama_format":
        payload["format"] = schema               # Ollama's native JSON-schema field
        payload.pop("response_format", None)     # avoid sending both
    elif style == "openai_json_schema":
        payload["response_format"] = {"type": "json_schema",
                                      "json_schema": {"name": "extraction", "schema": schema}}


def _openai_request(messages: list[dict], cfg: dict, args):
    url = f"{cfg['base_url']}/chat/completions"
    if cfg.get("no_system"):
        messages = _merge_system_into_user(messages)   # Gemma & friends: no system role
    payload = {"model": cfg["model"], "messages": messages,
               "temperature": 0, "max_tokens": args.max_tokens}
    if not getattr(args, "no_json_mode", False):
        payload["response_format"] = {"type": "json_object"}
    _attach_schema(payload, cfg, args)
    headers = {"Authorization": f"Bearer {cfg['key']}", "Content-Type": "application/json"}

    def extract(j):
        return j["choices"][0]["message"]["content"]
    return url, headers, payload, extract


def _anthropic_request(messages: list[dict], cfg: dict, args):
    # Anthropic's Messages API takes the system prompt as a top-level field. For
    # JSON extraction we prefill an assistant "{" to coax a bare object; for prose
    # (no_json_mode, e.g. the analyst narrative) we leave the turn open.
    json_mode = not getattr(args, "no_json_mode", False)
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    chat = [{"role": m["role"], "content": m["content"]} for m in messages if m["role"] != "system"]
    if json_mode:
        chat.append({"role": "assistant", "content": "{"})
    url = f"{cfg['base_url']}/messages"
    payload = {"model": cfg["model"], "max_tokens": args.max_tokens, "temperature": 0,
               "system": system, "messages": chat}
    headers = {"x-api-key": cfg["key"], "anthropic-version": "2023-06-01",
               "content-type": "application/json"}

    def extract(j):
        text = "".join(b.get("text", "") for b in j.get("content", []) if b.get("type") == "text")
        return ("{" + text) if json_mode else text   # reattach the prefilled brace in JSON mode
    return url, headers, payload, extract


def call_llm(messages: list[dict], args) -> str:
    import requests
    cfg = resolve_provider(args)
    builder = _anthropic_request if cfg["kind"] == "anthropic" else _openai_request
    url, headers, payload, extract = builder(messages, cfg, args)

    last_err = None
    for attempt in range(1, args.retries + 1):
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=args.timeout)
        except requests.RequestException as e:
            last_err = str(e)
        else:
            if resp.status_code in (429, 500, 502, 503, 504, 529):
                last_err = f"HTTP {resp.status_code}: {resp.text[:200]}"  # transient → retry
            elif resp.status_code in (400, 401, 403, 404, 422):
                detail = resp.text[:300]
                hint = ""
                if resp.status_code in (401, 403):
                    hint = (" — check the API key, or this network may be blocking the "
                            f"provider (the network policy must allow {cfg['base_url']}).")
                elif "model" in detail.lower():
                    hint = f" — model {cfg['model']!r} may be invalid for {cfg['name']}; pass --model."
                raise SystemExit(f"{cfg['name']} provider error HTTP {resp.status_code}: {detail}{hint}")
            else:
                resp.raise_for_status()
                return extract(resp.json())
        if attempt < args.retries:
            wait = 2 ** attempt
            print(f"   ⚠️  LLM call failed ({last_err}); retry {attempt}/{args.retries - 1} in {wait}s")
            time.sleep(wait)
    raise SystemExit(f"{cfg['name']} call failed after {args.retries} attempts: {last_err}")


def _strip_code_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        nl = t.find("\n")          # drop the ``` or ```json opening line
        if nl != -1:
            t = t[nl + 1:]
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()


def _first_json_object(text: str) -> str | None:
    """Return the first balanced {...} block, respecting JSON strings/escapes.

    More robust than slicing the outermost braces: tolerates a trailing brace
    that appears in prose after the object, or an opening brace inside a string.
    """
    start = text.find("{")
    if start == -1:
        return None
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def parse_llm_json(raw: str) -> dict:
    text = _strip_code_fences(raw)
    try:                            # fast path: the whole response is the object
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    candidate = _first_json_object(text)
    if candidate is None:
        raise ValueError("no JSON object found in model response")
    return json.loads(candidate)


# ── Normalization (map common LLM aliases to the contract) ───────────────────

_META_ALIASES = {
    "ticker": "symbol", "company": "company_name", "company_name": "company_name",
    "reporting_currency": "currency", "framework": "reporting_framework",
    "reporting_framework": "reporting_framework", "period_end": "period_end",
}
_UNIT_WORDS = {"thousand": 1000, "thousands": 1000, "million": 1000000, "millions": 1000000}


_DIMENSION_ALIASES = {
    "business": "business_line", "operating": "business_line", "operating_segment": "business_line",
    "segment": "business_line", "division": "business_line", "activity": "business_line",
    "geographic": "geography", "geographical": "geography", "country": "geography",
    "region": "geography", "location": "geography",
    "entity": "legal_entity", "subsidiary": "legal_entity", "company": "legal_entity",
}


def _normalize_dimension(val):
    if not val:
        return "business_line"
    s = str(val).strip().lower().replace(" ", "_").replace("-", "_")
    if s in SEGMENT_DIMENSIONS:
        return s
    return _DIMENSION_ALIASES.get(s, "business_line")


def _normalize_sector(val):
    if not val:
        return None
    s = str(val).strip().lower().replace(" ", "_").replace("-", "_")
    if s in SECTORS:
        return s
    if "islamic" in s:
        return "islamic_bank"
    if "bank" in s:
        return "conventional_bank"
    if "insur" in s or "takaful" in s:
        return "insurance"
    if "industr" in s:
        return "industrial"
    return "other"


def _normalize_unit_scale(val):
    if val in (1, 1000, 1000000):
        return val
    if isinstance(val, str):
        for word, scale in _UNIT_WORDS.items():
            if word in val.lower():
                return scale
    return None


def normalize_filing(d: dict) -> dict:
    if not isinstance(d, dict):
        return d
    meta = dict(d.get("metadata") or {})
    for alias, canon in _META_ALIASES.items():
        if alias in meta and canon not in meta:
            meta[canon] = meta.pop(alias)
    if "sector" in meta:
        meta["sector"] = _normalize_sector(meta.get("sector"))
    us = _normalize_unit_scale(meta.get("unit_scale"))
    if us is not None:
        meta["unit_scale"] = us
    d["metadata"] = meta

    audit = dict(d.get("audit") or {})
    if not audit.get("verbatim_text"):
        for alt in ("opinion_text", "opinion", "text"):
            if audit.get(alt):
                audit["verbatim_text"] = audit[alt]
                break
    kams = []
    for k in audit.get("key_audit_matters") or []:
        if isinstance(k, dict):
            title = k.get("title") or k.get("matter") or ""
            text = k.get("text") or k.get("description") or k.get("verbatim_text") or ""
            kams.append({"title": title, "text": text})
    audit["key_audit_matters"] = kams
    eom = audit.get("emphasis_of_matter")
    if isinstance(eom, str):
        audit["emphasis_of_matter"] = [eom] if eom.strip() else []
    elif eom is None:
        audit["emphasis_of_matter"] = []
    d["audit"] = audit

    unmapped: list[str] = []
    norm_statements = []
    for st in d.get("statements") or []:
        if not isinstance(st, dict):
            continue
        st = dict(st)
        if "period_label" not in st and "period" in st:
            st["period_label"] = st.pop("period")
        clean_items = []
        for li in (st.get("line_items") or []):
            if not (isinstance(li, dict) and li.get("label_verbatim")):
                continue
            # The model sometimes invents a plausible-looking code that isn't
            # canonical (e.g. IS_OTHER_COMPREHENSIVE_INCOME). account_code is
            # allowed to be null — the verbatim label is always kept, so this
            # stays lossless. Coerce unknowns to null and record them rather
            # than hard-failing the whole filing.
            code = li.get("account_code")
            if code is not None and code not in KNOWN_ACCOUNT_CODES:
                unmapped.append(f"{code} → {li.get('label_verbatim')}")
                li["account_code"] = None
            # Fold common prior-year aliases into the canonical `comparatives` list.
            if not li.get("comparatives"):
                pv = li.get("prior_value", li.get("previous_value", li.get("prior_year_value")))
                if pv is not None:
                    pl = (li.get("prior_period_label") or li.get("previous_period_label")
                          or li.get("prior_year") or "prior")
                    li["comparatives"] = [{"period_label": str(pl), "value": pv}]
            clean_items.append(li)
        st["line_items"] = clean_items
        norm_statements.append(st)
    d["statements"] = norm_statements

    if unmapped:
        eq = d.get("extraction_quality")
        if not isinstance(eq, dict):
            eq = {"confidence": None, "warnings": [], "unmapped_labels": []}
            d["extraction_quality"] = eq
        eq.setdefault("unmapped_labels", [])
        eq["unmapped_labels"].extend(unmapped)

    norm_segments = []
    for sg in d.get("segments") or []:
        if not (isinstance(sg, dict) and sg.get("name")):
            continue
        sg = dict(sg)
        sg["dimension"] = _normalize_dimension(sg.get("dimension"))
        norm_segments.append(sg)
    d["segments"] = norm_segments

    if not isinstance(d.get("notes"), list):
        d["notes"] = []
    if not isinstance(d.get("extraction_quality"), dict):
        d["extraction_quality"] = {"confidence": None, "warnings": [], "unmapped_labels": []}
    return d


# ── Merge partial filings from windows ───────────────────────────────────────

def _statement_score(st: dict) -> tuple[int, int]:
    return (len(st.get("line_items") or []), len(st.get("verbatim_text") or ""))


def _line_item_key(li: dict) -> tuple:
    label = " ".join(str(li.get("label_verbatim") or "").split()).lower()
    return (label, li.get("value"), str(li.get("note_ref") or ""))


def _merge_statement_group(stype: str, group: list[dict]) -> dict:
    """Combine every window's copy of one statement type into a single statement.

    The fullest single rendering supplies the scalar fields and verbatim_text
    (longest wins, which avoids duplicating the overlap region), but line_items
    are UNIONED across all windows and de-duplicated, so a statement split
    across a page/window boundary keeps every row — preserving the structured
    data losslessly rather than discarding the smaller partial.
    """
    primary = max(group, key=_statement_score)
    items: list[dict] = []
    seen: dict[tuple, int] = {}
    for st in group:
        for li in st.get("line_items") or []:
            if not (isinstance(li, dict) and li.get("label_verbatim")):
                continue
            key = _line_item_key(li)
            if key in seen:                       # overlap duplicate — keep one,
                kept = items[seen[key]]           # but upgrade a null code if a
                if not kept.get("account_code") and li.get("account_code"):
                    kept["account_code"] = li["account_code"]  # later copy mapped it
                if not kept.get("comparatives") and li.get("comparatives"):
                    kept["comparatives"] = li["comparatives"]  # or recovered comparatives
                continue
            seen[key] = len(items)
            items.append(dict(li))
    return {
        "type": stype,
        "title": primary.get("title"),
        "period_label": primary.get("period_label"),
        "verbatim_text": primary.get("verbatim_text") or "",
        "line_items": items,
    }


def merge_filings(parts: list[dict]) -> dict:
    merged = empty_filing()
    for part in parts:
        for k, v in (part.get("metadata") or {}).items():
            if v not in (None, "") and merged["metadata"].get(k) in (None, "", 1, "QAR"):
                merged["metadata"][k] = v
            elif v not in (None, "") and k not in merged["metadata"]:
                merged["metadata"][k] = v

    best_audit = None
    all_kams, all_eom = [], []
    for part in parts:
        a = part.get("audit") or {}
        all_kams.extend(a.get("key_audit_matters") or [])
        all_eom.extend(a.get("emphasis_of_matter") or [])
        opinion = a.get("opinion_type")
        has_real = (opinion and opinion != "unknown") or a.get("verbatim_text")
        if has_real and (best_audit is None or len(a.get("verbatim_text") or "") > len(best_audit.get("verbatim_text") or "")):
            best_audit = a
    if best_audit:
        merged["audit"].update({
            "opinion_type": best_audit.get("opinion_type") or "unknown",
            "auditor_name": best_audit.get("auditor_name"),
            "report_date": best_audit.get("report_date"),
            "verbatim_text": best_audit.get("verbatim_text") or "",
            "material_uncertainty_going_concern": best_audit.get("material_uncertainty_going_concern"),
        })
    seen_k, kams = set(), []
    for k in all_kams:
        key = (k.get("title") or "") + (k.get("text") or "")[:60]
        if key and key not in seen_k:
            seen_k.add(key)
            kams.append(k)
    merged["audit"]["key_audit_matters"] = kams
    merged["audit"]["emphasis_of_matter"] = sorted(set(e for e in all_eom if e and e.strip()))

    by_type: dict[str, list[dict]] = {}
    for part in parts:
        for st in part.get("statements") or []:
            t = st.get("type")
            if not t:
                continue
            by_type.setdefault(t, []).append(st)
    merged["statements"] = [_merge_statement_group(t, g) for t, g in by_type.items()]

    # Union segments across windows; the richest copy of each (dimension, name,
    # period) wins so a segment note split across a window boundary isn't lost.
    by_seg: dict[tuple, tuple] = {}
    for part in parts:
        for sg in part.get("segments") or []:
            if not (isinstance(sg, dict) and sg.get("name")):
                continue
            key = (sg.get("dimension"), " ".join(str(sg["name"]).split()).lower(),
                   sg.get("period_label"))
            score = (len(sg.get("metrics") or {}), len(sg.get("verbatim_text") or ""))
            if key not in by_seg or score > by_seg[key][0]:
                by_seg[key] = (score, sg)
    merged["segments"] = [v[1] for v in by_seg.values()]

    by_note: dict[str, dict] = {}
    for part in parts:
        for nt in part.get("notes") or []:
            key = (nt.get("number") or nt.get("title") or "").strip() or f"__{id(nt)}"
            cur = by_note.get(key)
            if cur is None or len(nt.get("verbatim_text") or "") > len(cur.get("verbatim_text") or ""):
                by_note[key] = nt
    merged["notes"] = list(by_note.values())

    warnings, confs, unmapped = [], [], []
    for part in parts:
        eq = part.get("extraction_quality") or {}
        warnings.extend(eq.get("warnings") or [])
        unmapped.extend(eq.get("unmapped_labels") or [])
        if isinstance(eq.get("confidence"), (int, float)):
            confs.append(eq["confidence"])
    merged["extraction_quality"] = {
        "confidence": min(confs) if confs else None,
        "warnings": sorted(set(warnings)),
        "unmapped_labels": sorted(set(unmapped)),
    }
    return merged


# ── Upload ────────────────────────────────────────────────────────────────────

def upload_filing(filing: dict, args, analysis: dict | None = None) -> dict:
    import requests
    base = args.api_url.rstrip("/")
    if base.startswith("http://") and not any(h in base for h in ("localhost", "127.0.0.1")):
        print("⚠️  uploading over plaintext HTTP to a non-local host — the ingest token "
              "would be exposed in transit; use an https:// QSCREEN_API_URL.")
    url = f"{base}/api/v1/ingest/filing"
    headers = {"Authorization": f"Bearer {args.token}", "Content-Type": "application/json"}
    # Additive: when asked, fold the derived analysis in as a sibling key. The
    # filing contract itself is unchanged, so a backend that ignores unknown keys
    # is unaffected.
    payload = filing if analysis is None else {**filing, "analysis": analysis}
    resp = requests.post(url, headers=headers, json=payload, timeout=180)
    resp.raise_for_status()
    return resp.json()


def build_analysis_artifacts(filing: dict, args) -> dict:
    """Derived analysis (+ valuation) for one filing — saved locally and/or folded
    into the upload. Lazily imported so the core engine stays standalone."""
    symbol = (filing.get("metadata") or {}).get("symbol") or getattr(args, "symbol", "")
    profile = getattr(args, "_profile", None)
    try:
        import qscreen_analyze
        import qscreen_dcf
    except ImportError as e:                      # analysis layer genuinely absent
        return {"analysis_error": f"analysis modules unavailable: {e}"}
    # Real bugs inside analyze()/value() propagate to the caller (who decides whether
    # to degrade) rather than being silently stringified here.
    return {
        "analysis": qscreen_analyze.analyze(symbol, [filing], profile),
        "valuation": qscreen_dcf.value(symbol, [filing], profile),
    }


# ════════════════════════════════════════════════════════════════════════════
#  GUIDED EXTRACTION  (for small / local models — e.g. a 2-bit Gemma on a laptop)
# ════════════════════════════════════════════════════════════════════════════
#
# A small model can't hold the whole 60-code contract in its head and emit one
# giant JSON object per 12-page window. So guided mode does the THINKING in
# Python and asks the model only to do the small, mechanical part it CAN do:
# read one short table and list its rows.
#
#   1. find statement titles in the page text          (deterministic regex)
#   2. for each statement, ask the model for its rows   (tiny flat schema)
#   3. map each row's label → a canonical account code  (deterministic rules)
#   4. read the unit scale, parse numbers, set audit    (deterministic + 1 ask)
#
# Every "rule" the big prompt used to spell out for the model is encoded here in
# code instead, so even a tiny context window is enough. The output is the same
# lossless filing contract, assembled and merged with the normal machinery.

GUIDED_DEFAULT_PAGES = 3      # small windows keep each ask inside a tiny context

# Standard IFRS / QSE statement headings → our statement type. Order matters:
# the most specific heading is tried first (a combined "profit or loss and other
# comprehensive income" page is classified as the income statement).
STATEMENT_TITLE_PATTERNS: list[tuple[str, str]] = [
    (r"statement of changes in (?:equity|shareholders)", "changes_in_equity"),
    (r"statement of cash ?flows?", "cash_flow"),
    (r"cash ?flow statement", "cash_flow"),
    (r"statement of financial position", "balance_sheet"),
    (r"balance sheet", "balance_sheet"),
    (r"statement of (?:profit or loss|income)", "income_statement"),
    (r"income statement", "income_statement"),
    (r"statement of comprehensive income", "comprehensive_income"),
    (r"statement of operations", "income_statement"),
]

# label keyword(s) → canonical account_code, grouped by the statement they live
# in so a balance-sheet row never grabs an income-statement code. Within a group
# the FIRST rule whose any-phrase appears in the (lowercased) label wins, so
# specific phrases are listed before generic ones ("net interest income" before
# "interest income"; "total liabilities and equity" before "total liabilities").
_LABEL_RULES: dict[str, list[tuple[str, tuple[str, ...]]]] = {
    "income": [
        ("IS_NET_INTEREST", ("net interest income", "net interest", "net financing income",
                             "net income from financing", "net profit from financing")),
        ("IS_INTEREST_INCOME", ("interest income", "income from financing", "financing income",
                                "income from islamic financing")),
        ("IS_INTEREST_EXP", ("interest expense", "finance cost", "financing cost",
                             "profit paid", "return to depositors", "depositors' share")),
        ("IS_FEES_COMM", ("fee and commission", "fees and commission", "net fee",
                          "commission income", "fee income")),
        ("IS_FX_GAIN", ("foreign exchange", "exchange gain", "fx gain")),
        ("IS_INVESTMENT_INCOME", ("investment income", "income from investment", "dividend income")),
        ("IS_GROSS_PREMIUMS", ("gross premium", "gross written premium")),
        ("IS_NET_PREMIUMS", ("net premium", "net earned premium")),
        ("IS_CLAIMS", ("claims incurred", "net claims", "gross claims", "claims paid", "claim")),
        ("IS_NET_ECL", ("expected credit loss", "impairment loss", "net impairment", "credit loss",
                        "impairment of", "impairment on", "provision for impairment", "ecl")),
        ("IS_OTHER_PROVISIONS", ("other provisions", "provision for")),
        ("IS_STAFF", ("staff cost", "personnel", "salaries", "employee benefit", "wages")),
        ("IS_DEPRECIATION", ("depreciation",)),
        ("IS_AMORT_INTANGIBLE", ("amortis", "amortiz")),
        ("IS_SHARE_ASSOCIATES", ("share of results of associate", "share of profit of associate",
                                 "share of associate", "associates and joint")),
        ("IS_OPERATING_PROFIT", ("operating profit", "profit from operations")),
        ("IS_PROFIT_BEFORE_TAX", ("profit before tax", "profit before income tax",
                                  "profit before zakat", "profit for the year before tax")),
        ("IS_INCOME_TAX", ("income tax", "tax expense", "zakat and tax", "taxation")),
        ("IS_NET_MONETARY", ("monetary position", "net monetary")),
        ("IS_NCI", ("non-controlling", "minority interest")),
        ("IS_EPS", ("earnings per share", "per share")),
        ("IS_NET_INCOME", ("profit for the year", "profit for the period", "net profit",
                           "profit attributable", "net income")),
        ("IS_OPERATING_EXP", ("operating expense", "general and admin", "other expenses",
                              "total expenses", "administrative expenses")),
        ("IS_OTHER_INCOME", ("other operating income", "other income")),
        ("IS_REVENUE", ("total revenue", "revenue from contracts", "revenue", "total income",
                        "operating income", "total operating income")),
    ],
    "balance": [
        ("BS_TLOE", ("total liabilities and equity", "total equity and liabilities",
                     "total liabilities and shareholders")),
        ("BS_TOTAL_LIABILITIES", ("total liabilities",)),
        ("BS_TOTAL_EQUITY", ("total equity", "total shareholders", "shareholders' equity",
                             "shareholders’ equity", "equity attributable to")),
        ("BS_TOTAL_ASSETS", ("total assets",)),
        ("BS_CASH", ("cash and balances", "cash and cash equivalent", "cash on hand", "cash and short")),
        ("BS_TREASURY", ("treasury bill", "with central bank", "with qatar central bank")),
        ("BS_DUE_FROM_BANKS", ("due from banks", "due from financial institution",
                               "placements with banks")),
        ("BS_TRADING_INVEST", ("held for trading", "trading investment")),
        ("BS_FVTPL", ("fair value through profit",)),
        ("BS_FVOCI", ("fair value through other comprehensive", "fair value through equity",
                      "available for sale", "available-for-sale")),
        ("BS_LOANS", ("loans and advances", "financing assets", "islamic financing",
                      "loans and financing", "financing and investing")),
        ("BS_SUKUK", ("sukuk financing", "sukuk")),
        ("BS_AT1", ("additional tier 1", "tier 1 capital", "tier i capital")),
        ("BS_DUE_TO_BANKS", ("due to banks", "due to financial institution", "deposits from banks")),
        ("BS_CUSTOMER_DEPOSITS", ("customer deposit", "customers' deposit", "customers’ deposit",
                                  "customer account", "customers' account", "customers’ account",
                                  "deposits from customers")),
        ("BS_SHARE_CAPITAL", ("share capital", "paid up capital", "paid-up capital")),
        ("BS_RETAINED", ("retained earnings", "accumulated profit", "accumulated losses")),
    ],
    "cash": [
        ("CF_OCF", ("operating activities", "cash from operating", "cash generated from operations")),
        ("CF_ICF", ("investing activities", "cash from investing")),
        ("CF_FCF", ("financing activities", "cash from financing")),
        ("CF_CAPEX", ("purchase of property", "acquisition of property", "capital expenditure",
                      "additions to property", "purchase of fixed assets")),
        ("CF_DIVIDENDS_PAID", ("dividends paid", "dividend paid")),
        ("CF_NET_CHANGE", ("net increase in cash", "net decrease in cash", "net change in cash")),
    ],
    "kpi": [
        ("KPI_CAR", ("capital adequacy",)),
        ("KPI_NPL", ("non-performing", "npl ratio")),
        ("KPI_NIM", ("net interest margin",)),
        ("KPI_COST_INCOME", ("cost to income", "cost-income", "cost income ratio", "efficiency ratio")),
        ("KPI_LDR", ("loan to deposit", "loans to deposit", "financing to deposit")),
        ("KPI_ROE", ("return on equity", "return on average equity")),
        ("KPI_ROA", ("return on assets", "return on average assets")),
        ("KPI_COVERAGE", ("coverage ratio", "provision coverage")),
        ("KPI_LOSS_RATIO", ("loss ratio",)),
        ("KPI_EXPENSE_RATIO", ("expense ratio",)),
        ("KPI_COMBINED", ("combined ratio",)),
    ],
}
# Which rule groups apply to each statement type (kpi codes can appear anywhere).
_GROUPS_FOR_STATEMENT = {
    "income_statement": ("income", "kpi"),
    "comprehensive_income": ("income", "kpi"),
    "balance_sheet": ("balance", "kpi"),
    "cash_flow": ("cash", "kpi"),
    "changes_in_equity": ("balance", "kpi"),
}


def map_label_to_code(label: str, stype: str | None = None) -> str | None:
    """Map a free-text statement-row label to a canonical account_code (or None).

    This is the deterministic 'rule' that replaces asking a small model to pick
    from 60 codes. Only ever returns a code in KNOWN_ACCOUNT_CODES, or None.
    """
    if not label:
        return None
    text = " ".join(str(label).split()).lower()
    groups = _GROUPS_FOR_STATEMENT.get(stype or "", tuple(_LABEL_RULES))
    for group in groups:
        for code, phrases in _LABEL_RULES.get(group, ()):
            if any(ph in text for ph in phrases):
                return code if code in KNOWN_ACCOUNT_CODES else None
    return None


def detect_statement_titles(text: str) -> list[tuple[str, str, int]]:
    """Find financial-statement headings in page text.

    Returns (statement_type, matched_title, start_index) sorted by position, at
    most one entry per statement type (the first occurrence), so we can slice the
    text around each statement and feed the model just that.
    """
    found: dict[str, tuple[str, int]] = {}
    for pat, stype in STATEMENT_TITLE_PATTERNS:
        if stype in found:
            continue
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            found[stype] = (m.group(0).strip(), m.start())
    return sorted(((st, t, i) for st, (t, i) in found.items()), key=lambda x: x[2])


_UNIT_SCALE_PATTERNS = [
    (1000000, re.compile(r"in\s+millions|\bQAR?\s*'?\s*000\s*'?\s*000|\bmillions of\b", re.IGNORECASE)),
    (1000,    re.compile(r"in\s+thousands|thousands of|\bQAR?\s*'?\s*000\b|\b'000\b", re.IGNORECASE)),
]


def detect_unit_scale(text: str) -> int | None:
    """Read 'in thousands' / 'in millions' / "QR'000" from a statement header."""
    head = text[:4000]   # the scale note sits at the top of the statement
    for scale, pat in _UNIT_SCALE_PATTERNS:
        if pat.search(head):
            return scale
    return None


_NUM_RE = re.compile(r"-?\d[\d,]*\.?\d*")


def _coerce_number(x) -> float | int | None:
    """Turn whatever a small model emits for a number into a float/int (or None).

    Handles "1,234", "(56)" → -56, "12.3%", "QAR 1,000", "—"/"-"/"n/a" → None.
    """
    if x is None or isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        return x
    s = str(x).strip()
    if not s or s in {"-", "—", "–"} or s.lower() in {"n/a", "na", "nil", "none", "null"}:
        return None
    neg = (s.startswith("(") and s.endswith(")")) or s.startswith("-")
    m = _NUM_RE.search(s.replace("(", "-"))
    if not m:
        return None
    try:
        val = float(m.group(0).replace(",", ""))
    except ValueError:
        return None
    if neg and val > 0:
        val = -val
    return int(val) if val == int(val) else val


def _coerce_opinion(s: str | None) -> str:
    """Map free text to a canonical audit opinion_type (default 'unknown')."""
    t = (s or "").lower()
    if "unqualif" in t or "unmodified" in t:
        return "unqualified"
    if "advers" in t:
        return "adverse"
    if "disclaim" in t:
        return "disclaimer"
    if "review" in t:
        return "review"
    if "qualif" in t or "modified" in t or "except for" in t:
        return "qualified"
    return "unknown"


_AUDIT_HINT = re.compile(
    r"independent auditor|auditor'?s report|in our opinion|report on the audit|"
    r"report of the (?:independent )?auditor", re.IGNORECASE)
_NOTES_HINT = re.compile(
    r"notes? to the (?:consolidated )?financial statements|significant accounting policies|"
    r"\bnote\s+\d+\b", re.IGNORECASE)

_NOTE_CATEGORY_HINTS: list[tuple[str, tuple[str, ...]]] = [
    ("accounting_policies", ("accounting policies", "basis of preparation")),
    ("critical_estimates", ("critical estimates", "significant judgements", "key sources of estimation")),
    ("segment_information", ("segment",)),
    ("contingent_liabilities", ("contingent", "contingencies", "legal claims")),
    ("commitments", ("commitments", "capital commitments", "credit-related commitments")),
    ("related_party", ("related part",)),
    ("subsequent_events", ("subsequent event", "events after the reporting")),
    ("going_concern", ("going concern",)),
    ("fair_value", ("fair value",)),
    ("capital_adequacy", ("capital adequacy", "capital management")),
    ("ecl_provisions", ("expected credit loss", "impairment", "ecl", "staging")),
    ("sukuk_islamic", ("sukuk", "wakala", "mudaraba", "murabaha", "ijara", "quasi-equity")),
    ("insurance_technical", ("insurance contract", "technical provision", "claims development")),
    ("financial_instruments_risk", ("risk management", "credit risk", "liquidity risk", "market risk")),
    ("other_comprehensive_income", ("comprehensive income",)),
    ("cost_breakdown", ("general and administrative", "other operating expenses")),
    ("other_income", ("other income",)),
]


def _note_category(title: str) -> str:
    t = (title or "").lower()
    for cat, phrases in _NOTE_CATEGORY_HINTS:
        if any(ph in t for ph in phrases):
            return cat
    return "other"


def _slice_statements(text: str, titles: list[tuple[str, str, int]]) -> list[tuple[str, str, str]]:
    """Cut the window text into one chunk per detected statement.

    Returns (statement_type, title, chunk_text); each chunk runs from its title
    to the next statement's title, so the model sees only that one table.
    """
    out = []
    for idx, (stype, title, start) in enumerate(titles):
        end = titles[idx + 1][2] if idx + 1 < len(titles) else len(text)
        out.append((stype, title, text[start:end].strip()))
    return out


# ── Deterministic table extraction (numbers read in code, not by the model) ──
#
# pdf_to_pages() flattens pdfplumber's tables into the page text as a block:
#   [TABLES on page N]
#   -- table 1 --
#   Total assets | 1,000,000 | 900,000
#   ...
# We parse that grid straight back into line items — so even a 270M model never
# has to read a single number. This is the backbone of "Basic" mode.

_TABLES_HDR_RE = re.compile(r"\[TABLES on page (\d+)\]")
_TABLE_SEP_RE = re.compile(r"^-- table \d+ --$")
# A bare note-reference cell: a small 1-3 digit integer (e.g. "12", "7a", "(8)")
# printed between the label and the money columns — NOT a value.
_NOTE_REF_CELL_RE = re.compile(r"^\(?\d{1,3}[a-z]?\)?$", re.IGNORECASE)


def parse_rendered_tables(text: str) -> list[dict]:
    """Recover the table grid that _render_tables() flattened into page text.

    Returns [{"page": int, "start": int, "rows": [[cell, ...], ...]}], one entry
    per `-- table k --` table. `start` is the char offset of the block header (used
    to bind a table to the statement title that precedes it). Cells keep their
    verbatim text, including empty "" cells, by splitting on the literal " | ".
    """
    out: list[dict] = []
    headers = list(_TABLES_HDR_RE.finditer(text))
    for hi, m in enumerate(headers):
        page, start, body_start = int(m.group(1)), m.start(), m.end()
        # the block ends at the next page delimiter, the next TABLES header, or EOF
        ends = [len(text)]
        nxt = text.find("\n===== PAGE", body_start)
        if nxt != -1:
            ends.append(nxt)
        if hi + 1 < len(headers):
            ends.append(headers[hi + 1].start())
        block = text[body_start:min(ends)]
        tables: list[list[list[str]]] = []
        for raw in block.splitlines():
            line = raw.strip()
            if not line:
                continue
            if _TABLE_SEP_RE.match(line):
                tables.append([])
            elif tables:
                tables[-1].append(raw.split(" | "))
        for rows in tables:
            if rows:
                out.append({"page": page, "start": start, "rows": rows})
    return out


def _row_to_triplet(cells: list[str]) -> dict | None:
    """One rendered table row → {label, current, prior, note_ref} (or None).

    label = first cell containing a letter; numbers = cells to its right that
    _coerce_number parses. A leading small-integer note-ref column is demoted
    (only when ≥3 numbers remain, so a real current+prior is never lost).
    """
    cells = [(c if c is not None else "") for c in cells]
    label_idx = next((i for i, c in enumerate(cells) if re.search(r"[A-Za-z]", c)), None)
    if label_idx is None:
        return None
    label = cells[label_idx].strip()
    if not label:
        return None
    nums = [(c.strip(), _coerce_number(c)) for c in cells[label_idx + 1:]]
    nums = [(raw, v) for raw, v in nums if v is not None]
    note_ref = None
    if len(nums) >= 3 and _NOTE_REF_CELL_RE.match(nums[0][0]):
        note_ref = nums.pop(0)[0]
    current = nums[0][1] if nums else None
    prior = nums[1][1] if len(nums) > 1 else None
    return {"label": label, "current": current, "prior": prior, "note_ref": note_ref}


def _assign_table_stype(table_start: int,
                        titles: list[tuple[str, str, int]]) -> tuple[str, str] | None:
    """Bind a parsed table to the statement whose title most recently precedes it
    (else the nearest following title, else None → the table is skipped)."""
    preceding = [(s, t, i) for (s, t, i) in titles if i <= table_start]
    if preceding:
        s, t, _ = max(preceding, key=lambda x: x[2])
        return (s, t)
    following = [(s, t, i) for (s, t, i) in titles if i > table_start]
    if following:
        s, t, _ = min(following, key=lambda x: x[2])
        return (s, t)
    return None


def deterministic_statements(text: str, titles: list[tuple[str, str, int]],
                             prior_label: str, period_label: str | None) -> dict:
    """Build typed statement dicts straight from a window's recovered tables.

    Returns {stype: statement_dict} in the same shape the LLM path emits. Empty
    when there are no [TABLES ...] blocks — so the plain-text path falls through
    to the model unchanged. Line items are tagged basis="parsed".
    """
    tables = parse_rendered_tables(text)
    if not tables:
        return {}
    slices = {st: chunk for (st, _t, chunk) in _slice_statements(text, titles)}
    title_for = {st: t for (st, t, _i) in titles}
    acc: dict[str, dict] = {}
    seen: dict[str, set] = {}
    for tbl in tables:
        assoc = _assign_table_stype(tbl["start"], titles)
        if not assoc:
            continue
        stype, title = assoc
        for cells in tbl["rows"]:
            tri = _row_to_triplet(cells)
            if not tri:
                continue
            if tri["current"] is None and tri["prior"] is None \
                    and not map_label_to_code(tri["label"], stype):
                continue                       # a header / spacer row with no figures
            li = _build_line_item({"label": tri["label"], "current": tri["current"],
                                   "prior": tri["prior"]}, stype, prior_label,
                                  note_ref=tri["note_ref"], basis="parsed")
            if not li:
                continue
            key = (" ".join(tri["label"].split()).lower(), li["value"])
            if key in seen.setdefault(stype, set()):
                continue
            seen[stype].add(key)
            if stype not in acc:
                acc[stype] = {"type": stype, "title": title or stype.replace("_", " "),
                              "period_label": period_label,
                              "verbatim_text": slices.get(stype) or text, "line_items": []}
            acc[stype]["line_items"].append(li)
    return {st: s for st, s in acc.items() if s["line_items"]}


# ── Guided model asks (tiny prompts, tiny schemas) ───────────────────────────

# JSON schemas for the three small asks. Sent to runtimes that can enforce them
# (Ollama, LM Studio) via _attach_schema; ignored elsewhere (we still parse robustly).
_ROWS_SCHEMA = {"type": "object", "required": ["rows"], "properties": {
    "rows": {"type": "array", "items": {"type": "object", "required": ["label"], "properties": {
        "label": {"type": "string"},
        "current": {"type": ["number", "null"]},
        "prior": {"type": ["number", "null"]}}}}}}
_AUDIT_SCHEMA = {"type": "object", "required": ["opinion"], "properties": {
    "opinion": {"type": "string",
                "enum": ["unqualified", "qualified", "adverse", "disclaimer", "review", "unknown"]},
    "auditor": {"type": ["string", "null"]}}}
_NOTES_SCHEMA = {"type": "object", "required": ["notes"], "properties": {
    "notes": {"type": "array", "items": {"type": "object", "required": ["title"], "properties": {
        "number": {"type": ["string", "null"]}, "title": {"type": "string"}}}}}}


def _call_with_schema(messages: list[dict], args, schema: dict) -> str:
    """call_llm with an ephemeral response schema (honored only where supported)."""
    prev = getattr(args, "_schema", None)
    args._schema = schema
    try:
        return call_llm(messages, args)
    finally:
        args._schema = prev


def _guided_rows_messages(chunk: str, stype: str, title: str) -> list[dict]:
    pretty = stype.replace("_", " ")
    system = ("You read ONE financial table from a company report and list its rows as JSON. "
              "Copy numbers exactly as printed. A number in (brackets) is negative. "
              "Output ONLY a JSON object, nothing else.")
    user = (f'This table is the "{title}" (a {pretty}). List EVERY line that has a number.\n'
            'For each line give its label and up to two numbers: the current period and the '
            'previous period (prior-year column). If a line shows only one number, set "prior" to null.\n'
            'Reply EXACTLY in this shape:\n'
            '{"rows":[{"label":"Total assets","current":123,"prior":110}]}\n\n'
            f"TABLE:\n{chunk}")
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _rows_from_response(obj: dict) -> list[dict]:
    rows = obj.get("rows")
    if not isinstance(rows, list):
        for alt in ("line_items", "items", "lines", "data"):
            if isinstance(obj.get(alt), list):
                rows = obj[alt]
                break
    return rows if isinstance(rows, list) else []


def guided_extract_rows(chunk: str, stype: str, title: str, args) -> list[dict]:
    """One model ask → a list of {label, current, prior} dicts (best-effort)."""
    raw = _call_with_schema(_guided_rows_messages(chunk, stype, title), args, _ROWS_SCHEMA)
    try:
        obj = parse_llm_json(raw)
    except (ValueError, json.JSONDecodeError):
        return []
    out = []
    for r in _rows_from_response(obj):
        if not isinstance(r, dict):
            continue
        label = r.get("label") or r.get("name") or r.get("item")
        if not label:
            continue
        cur = _coerce_number(r.get("current", r.get("value", r.get("amount"))))
        prior = _coerce_number(r.get("prior", r.get("previous", r.get("comparative")))) if any(
            k in r for k in ("prior", "previous", "comparative")) else None
        out.append({"label": str(label).strip(), "current": cur, "prior": prior})
    return out


def _build_line_item(row: dict, stype: str, prior_label: str,
                     note_ref: str | None = None, basis: str = "llm") -> dict | None:
    label = row.get("label")
    if not label:
        return None
    li = {
        "account_code": map_label_to_code(label, stype),
        "label_verbatim": label,
        "value": row.get("current"),
        "comparatives": [],
        "note_ref": note_ref,
        "depth": 0,
        "is_subtotal": False,
        "basis": basis,           # "parsed" = read from a PDF table; "llm" = from the model
    }
    if row.get("prior") is not None:
        li["comparatives"] = [{"period_label": prior_label, "value": row["prior"]}]
    return li


def _guided_audit_messages(chunk: str) -> list[dict]:
    system = ("You read the independent auditor's report from a company filing and answer as JSON. "
              "Output ONLY a JSON object.")
    user = ('From the auditor\'s report below, what is the opinion and who signed it?\n'
            'opinion must be one of: unqualified, qualified, adverse, disclaimer, review.\n'
            'Reply EXACTLY: {"opinion":"unqualified","auditor":"KPMG"}\n'
            'If there is no auditor report here, reply {"opinion":"unknown","auditor":null}.\n\n'
            f"TEXT:\n{chunk[:6000]}")
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def guided_extract_audit(chunk: str, args) -> dict:
    """Best-effort audit opinion from one model ask; verbatim_text stays lossless."""
    audit = {"opinion_type": "unknown", "auditor_name": None, "verbatim_text": "",
             "emphasis_of_matter": [], "key_audit_matters": [],
             "material_uncertainty_going_concern": {"present": False, "text": ""}}
    try:
        obj = parse_llm_json(_call_with_schema(_guided_audit_messages(chunk), args, _AUDIT_SCHEMA))
    except (ValueError, json.JSONDecodeError):
        return audit
    opinion = _coerce_opinion(obj.get("opinion") or obj.get("opinion_type"))
    audit["opinion_type"] = opinion
    auditor = obj.get("auditor") or obj.get("auditor_name")
    audit["auditor_name"] = str(auditor).strip() if auditor else None
    if opinion != "unknown":
        audit["verbatim_text"] = chunk      # keep the source text (validator needs it)
    return audit


def _guided_notes_messages(chunk: str) -> list[dict]:
    system = ("You list the numbered accounting notes on these report pages as JSON. "
              "Output ONLY a JSON object.")
    user = ('List each note that appears below by its number and title.\n'
            'Reply EXACTLY: {"notes":[{"number":"5","title":"Contingent liabilities"}]}\n'
            'If there are no notes here, reply {"notes":[]}.\n\n'
            f"TEXT:\n{chunk[:6000]}")
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def guided_extract_notes(chunk: str, args) -> list[dict]:
    """Best-effort note list (number + title); category assigned by local rules."""
    try:
        obj = parse_llm_json(_call_with_schema(_guided_notes_messages(chunk), args, _NOTES_SCHEMA))
    except (ValueError, json.JSONDecodeError):
        return []
    raw = obj.get("notes") if isinstance(obj.get("notes"), list) else []
    notes = []
    for n in raw:
        if not isinstance(n, dict):
            continue
        title = n.get("title") or n.get("name")
        if not title:
            continue
        notes.append({
            "number": str(n.get("number") or "").strip() or None,
            "title": str(title).strip(),
            "category": _note_category(title),
            "structured": {},
            "verbatim_text": chunk,          # lossless: keep the page text
        })
    return notes


def extract_filing_guided(pages: list[dict], args) -> dict:
    """Basic mode: deterministic-first extraction for small / local models.

    For each small window it (1) reads line items straight from the PDF's
    recovered tables — no model needed for the numbers — and only (2) falls back
    to a tiny per-table model ask for a statement whose title has no parseable
    table. Audit opinion is read deterministically first, then a closed-set model
    ask. With --no-llm the model is never called at all. Output is the standard
    lossless contract; line items carry basis "parsed" vs "llm".
    """
    size = min(getattr(args, "pages_per_chunk", None) or GUIDED_DEFAULT_PAGES, GUIDED_DEFAULT_PAGES)
    overlap = min(getattr(args, "overlap", 1) or 0, max(0, size - 1))
    windows = page_windows(pages, size, overlap)
    want_notes = bool(getattr(args, "guided_notes", False))
    no_llm = bool(getattr(args, "no_llm", False))
    prior_label = str(int(args.year) - 1) if getattr(args, "year", None) else "prior"
    period_label = str(args.year) if getattr(args, "year", None) else None
    how = "deterministic only (no model)" if no_llm else "deterministic-first, model fills gaps"
    print(f"🧭 Basic extraction: {len(windows)} small window(s) of ≤{size} page(s) — {how} …")

    parts: list[dict] = []
    for wi, win in enumerate(windows, 1):
        text = render_window(win)
        part = empty_filing()
        scale = detect_unit_scale(text)
        if scale:
            part["metadata"]["unit_scale"] = scale

        titles = detect_statement_titles(text)
        # 1) numbers straight from the recovered tables (no model)
        det = deterministic_statements(text, titles, prior_label, period_label)
        for stmt in det.values():
            part["statements"].append(stmt)
        # 2) only where a statement title had no parseable table, ask the model
        if not no_llm:
            for stype, title, chunk in _slice_statements(text, titles):
                if stype in det:
                    continue
                rows = guided_extract_rows(chunk, stype, title, args)
                items = [li for li in (_build_line_item(r, stype, prior_label) for r in rows) if li]
                if items:
                    part["statements"].append({
                        "type": stype, "title": title, "period_label": period_label,
                        "verbatim_text": chunk or text, "line_items": items,
                    })
        names = ", ".join(f"{s['type']}×{len(s['line_items'])}" for s in part["statements"]) or "—"
        print(f"   • window {wi}/{len(windows)} (pages {win[0]['num']}-{win[-1]['num']}): {names}")

        # audit: deterministic opinion first, then a closed-set model ask
        if _AUDIT_HINT.search(text):
            op = _coerce_opinion(text)
            if op != "unknown":
                part["audit"].update({"opinion_type": op, "verbatim_text": text})
            elif not no_llm:
                part["audit"] = guided_extract_audit(text, args)
        if want_notes and not no_llm and _NOTES_HINT.search(text):
            part["notes"] = guided_extract_notes(text, args)
        parts.append(normalize_filing(part))

    merged = merge_filings(parts)
    all_li = [li for s in merged.get("statements", []) for li in s.get("line_items", [])]
    n_parsed = sum(1 for li in all_li if li.get("basis") == "parsed")
    n_llm = sum(1 for li in all_li if li.get("basis") == "llm")
    n_codes = sum(1 for li in all_li if li.get("account_code"))
    note = f"{len(all_li)} line item(s): {n_parsed} parsed from tables, {n_llm} from the model"
    eq = merged.setdefault("extraction_quality", {"confidence": None, "warnings": [], "unmapped_labels": []})
    eq.setdefault("warnings", []).append(note)
    if eq.get("confidence") is None and all_li:        # mostly-parsed ⇒ high confidence
        eq["confidence"] = round(0.6 + 0.39 * (n_parsed / len(all_li)), 2)
    print(f"🧩 Assembled {len(merged.get('statements', []))} statement(s); {note}; "
          f"{n_codes} mapped to account codes.")
    return merged


def resolve_guided(args, cfg: dict) -> bool:
    """Decide whether to use guided (Basic) mode. Explicit flags win; otherwise it
    is ON by default for local runtimes (small models) and OFF for cloud ones."""
    if getattr(args, "no_guided", False):
        return False
    if getattr(args, "guided", False):
        return True
    env = os.getenv("QSCREEN_GUIDED")
    if env is not None:
        return env.strip().lower() in ("1", "true", "yes", "on")
    return bool(cfg.get("local"))


def apply_mode(args) -> None:
    """Map the friendly --mode / --basic / --pro / --no-llm onto the guided flags.
    Basic == guided (deterministic-first); Pro == the single big-prompt path."""
    mode = (getattr(args, "mode", None) or "").lower()
    if getattr(args, "no_llm", False) or getattr(args, "basic", False) or mode == "basic":
        args.guided = True
    elif getattr(args, "pro", False) or mode == "pro":
        args.no_guided = True
    # mode "auto" / unset → leave --guided/--no-guided for resolve_guided to decide


# ── Orchestration ─────────────────────────────────────────────────────────────

def extract_filing(pages: list[dict], args) -> dict:
    if getattr(args, "guided", False):
        return extract_filing_guided(pages, args)
    if args.no_chunk or len(pages) <= args.pages_per_chunk:
        print("🤖 Extracting (single pass) …")
        return normalize_filing(parse_llm_json(call_llm(build_messages(render_window(pages), args, windowed=False), args)))
    windows = page_windows(pages, args.pages_per_chunk, args.overlap)
    print(f"🤖 Extracting in {len(windows)} windows of ~{args.pages_per_chunk} pages (overlap {args.overlap}) …")
    parts = []
    for wi, win in enumerate(windows, 1):
        hint = f"pages {win[0]['num']}-{win[-1]['num']}"
        print(f"   • window {wi}/{len(windows)} ({hint})")
        raw = call_llm(build_messages(render_window(win), args, windowed=True, page_hint=hint), args)
        try:
            parts.append(normalize_filing(parse_llm_json(raw)))
        except (ValueError, json.JSONDecodeError) as e:
            print(f"     ⚠️  window {wi} returned unparseable JSON ({e}); skipping")
    if not parts:
        raise SystemExit("all windows failed to parse — nothing extracted")
    print(f"🧩 Merging {len(parts)} partial extracts …")
    return merge_filings(parts)


# ── Self-test (offline; no PDF, no API key, no network) ──────────────────────

def run_self_test() -> int:
    print("🧪 self-test: contract + normalize + merge …")
    good = empty_filing()
    good["metadata"].update({"symbol": "QNBK", "sector": "conventional_bank",
                             "fiscal_year": 2023, "fiscal_period": "FY", "unit_scale": 1000})
    good["audit"].update({"opinion_type": "unqualified", "verbatim_text": "In our opinion …"})
    good["statements"].append({"type": "income_statement", "title": "Income", "period_label": "2023",
                               "line_items": [{"account_code": "IS_NET_INTEREST", "label_verbatim": "NII",
                                               "value": 1, "note_ref": "24", "depth": 0, "is_subtotal": False}],
                               "verbatim_text": "NII 1"})
    good["notes"].append({"number": "27", "title": "Contingencies", "category": "contingent_liabilities",
                          "structured": {}, "verbatim_text": "…"})
    if validate_filing(good):
        print("❌ valid filing rejected:", validate_filing(good)); return 1

    drifted = {
        "metadata": {"ticker": "QIBK", "company": "Qatar Islamic Bank", "sector": "Islamic Bank",
                     "reporting_currency": "QAR", "framework": "AAOIFI", "unit_scale": 1000},
        "audit": {"opinion_type": "unqualified", "opinion_text": "In our opinion …",
                  "key_audit_matters": [{"title": "ECL", "description": "judgemental ECL"}]},
        "statements": [{"type": "income_statement", "period": "year_ended_2024", "verbatim_text": "…",
                        "line_items": [{"label_verbatim": "x", "value": 1},
                                       {"account_code": "IS_OTHER_COMPREHENSIVE_INCOME",
                                        "label_verbatim": "OCI", "value": 2}]}],
        "notes": [], "extraction_quality": {},
    }
    n = normalize_filing(drifted)
    checks = {
        "ticker→symbol": n["metadata"].get("symbol") == "QIBK",
        "company→company_name": n["metadata"].get("company_name") == "Qatar Islamic Bank",
        "sector normalized": n["metadata"].get("sector") == "islamic_bank",
        "currency alias": n["metadata"].get("currency") == "QAR",
        "framework alias": n["metadata"].get("reporting_framework") == "AAOIFI",
        "opinion_text→verbatim_text": bool(n["audit"].get("verbatim_text")),
        "KAM description→text": n["audit"]["key_audit_matters"][0].get("text") == "judgemental ECL",
        "period→period_label": n["statements"][0].get("period_label") == "year_ended_2024",
        "unknown code→null": n["statements"][0]["line_items"][1].get("account_code") is None,
        "unknown code recorded": any("IS_OTHER_COMPREHENSIVE_INCOME" in u
                                     for u in n["extraction_quality"].get("unmapped_labels", [])),
    }
    for name, ok in checks.items():
        if not ok:
            print(f"❌ normalize failed: {name}"); return 1

    a = empty_filing(); a["audit"].update({"opinion_type": "unqualified", "verbatim_text": "op …"})
    b = empty_filing(); b["statements"].append({"type": "balance_sheet", "verbatim_text": "BS …",
                                                "line_items": [{"label_verbatim": "Total assets", "value": 9, "account_code": "BS_TOTAL_ASSETS"}]})
    c = empty_filing(); c["notes"].append({"number": "5", "title": "Sukuk", "category": "sukuk_islamic",
                                          "structured": {}, "verbatim_text": "sukuk …"})
    m = merge_filings([a, b, c])
    if m["audit"]["opinion_type"] != "unqualified" or not m["statements"] or not m["notes"]:
        print("❌ merge failed to combine windows"); return 1

    print(f"✅ self-test passed — contract + normalize ({len(checks)} aliases) + merge all OK "
          f"({len(KNOWN_ACCOUNT_CODES)} codes, {len(NOTE_CATEGORIES)} note categories).")
    return 0


# ── Exports (flattened line-items table for human review) ────────────────────

EXPORT_COLUMNS = ["statement_type", "statement_title", "period_label", "account_code",
                  "label_verbatim", "value", "prior_period_label", "prior_value",
                  "note_ref", "depth", "is_subtotal"]


def flatten_line_items(filing: dict) -> list[dict]:
    rows: list[dict] = []
    for st in filing.get("statements") or []:
        for li in st.get("line_items") or []:
            comps = li.get("comparatives")
            comps = comps if isinstance(comps, list) else []   # tolerate a non-list from the LLM
            prior = comps[0] if comps and isinstance(comps[0], dict) else {}
            rows.append({
                "statement_type": st.get("type"),
                "statement_title": st.get("title"),
                "period_label": st.get("period_label"),
                "account_code": li.get("account_code"),
                "label_verbatim": li.get("label_verbatim"),
                "value": li.get("value"),
                "prior_period_label": prior.get("period_label"),
                "prior_value": prior.get("value"),
                "note_ref": li.get("note_ref"),
                "depth": li.get("depth"),
                "is_subtotal": li.get("is_subtotal"),
            })
    return rows


def export_csv(filing: dict, path: str) -> int:
    import csv
    rows = flatten_line_items(filing)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=EXPORT_COLUMNS)
        w.writeheader()
        w.writerows(rows)
    return len(rows)


# ── Main ──────────────────────────────────────────────────────────────────────

def save_json(filing: dict, args) -> str:
    out = f"{args.symbol.upper()}_{args.year}_{args.period}_filing.json"
    Path(out).write_text(json.dumps(filing, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"💾 Saved qscreen-uploadable file → {out}")
    return out


def write_outputs(filing: dict, args) -> tuple[list[str], dict | None]:
    """Write the opt-in local artifacts for a freshly-extracted filing — exports
    (csv/xlsx/html), analysis/valuation JSON, and the analyst report. Pure file I/O
    with no network, so it is unit-testable offline. Optional steps never raise
    (a failure is surfaced but cannot sink a good extraction). Returns
    (files_written, analysis_artifacts)."""
    base = f"{args.symbol.upper()}_{args.year}_{args.period}"
    written: list[str] = []

    for fmt in (getattr(args, "export", None) or []):
        if fmt == "csv":
            out = f"{base}_filing.csv"
            print(f"📑 Exported {export_csv(filing, out)} line item(s) → {out}")
        elif fmt == "xlsx":
            out = f"{base}_filing.xlsx"           # the multi-sheet workbook transcript
            import qscreen_workbook
            qscreen_workbook.save_workbook(filing, out)
            print(f"📑 Exported Excel transcript → {out}")
        else:                                    # html → printable statements document
            out = f"{base}_statements.html"
            import qscreen_statements
            qscreen_statements.save_statements_html(filing, out)
            print(f"📄 Exported statements document → {out}")
        written.append(out)

    # Optionally also persist the derived analysis/valuation locally.
    artifacts = None
    if getattr(args, "analyze", False) or getattr(args, "with_analysis", False):
        try:
            artifacts = build_analysis_artifacts(filing, args)
        except Exception as e:
            print(f"   ⚠️  analysis step failed (extraction is unaffected): {e}")
    if getattr(args, "analyze", False) and artifacts:
        if artifacts.get("analysis"):
            p = f"{base}_analysis.json"
            Path(p).write_text(json.dumps(artifacts["analysis"], indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"🧮 Saved analysis → {p} ({len(artifacts['analysis'].get('red_flags', []))} red flag(s))")
            written.append(p)
        if (artifacts.get("valuation") or {}).get("valuation"):
            p = f"{base}_valuation.json"
            Path(p).write_text(json.dumps(artifacts["valuation"], indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"💰 Saved valuation → {p} ({artifacts['valuation']['valuation']['model']})")
            written.append(p)

    # Optionally also render the one-page analyst report (HTML + Markdown).
    if getattr(args, "report", False):
        try:
            import qscreen_report
            rep = qscreen_report.build_report(
                args.symbol.upper(), [filing], getattr(args, "_profile", None),
                price=getattr(args, "price", None), shares=getattr(args, "shares", None))
            for ext, content in (("html", rep["html"]), ("md", rep["markdown"])):
                p = f"{base}_report.{ext}"
                Path(p).write_text(content, encoding="utf-8")
                written.append(p)
            print(f"📰 Analyst report → {base}_report.html (+ .md)")
        except Exception as e:
            print(f"   ⚠️  report step failed (extraction is unaffected): {e}")

    return written, artifacts


def run_filing(args) -> int:
    """Extract one PDF → save (+ optional export) → optionally upload. Returns
    an exit code: 0 ok, 2 saved-but-non-conforming (not uploaded)."""
    apply_mode(args)                    # --basic/--pro/--mode/--no-llm → guided flags
    no_llm = bool(getattr(args, "no_llm", False))
    try:
        cfg = resolve_provider(args)    # fail fast on bad provider/key before any work
    except SystemExit:
        if no_llm:
            cfg = deterministic_cfg()   # fully offline — no provider needed at all
        else:
            raise
    args.guided = resolve_guided(args, cfg)   # small/local models → Basic by default
    if no_llm:
        args.guided = True              # the deterministic-first orchestrator lives in Basic
    if qatar is not None and getattr(args, "symbol", None):
        args._profile = qatar.profile_for_year(args.symbol, getattr(args, "year", None))
        if args._profile:
            print(f"🇶🇦 Qatar profile: {args._profile.get('name_as_of')} "
                  f"[{args._profile.get('archetype')}] — "
                  f"{len(args._profile.get('active_events') or [])} regime/event(s) in force by {args.year}")
    mode = ("basic — deterministic, no model" if no_llm
            else "basic (deterministic-first)" if args.guided else "pro (single big prompt)")
    print(f"📄 Reading {Path(args.pdf).name} …  (provider: {cfg['name']}, model: {cfg['model']}, "
          f"mode: {mode})")
    if not args.guided and cfg.get("local"):
        print("   ⚠️  Pro mode leans on the model heavily — for best results use a strong model "
              "(GPT-4.5+/Claude Sonnet 4+/MiniMax-M2), or switch to Basic (--basic) for this small one.")
    pages, sha = pdf_to_pages(args.pdf, args.ocr)
    total_chars = sum(len(pg["text"]) for pg in pages)
    print(f"   {len(pages)} pages, {total_chars:,} chars (text + recovered tables), sha256={sha[:12]}…")

    filing = extract_filing(pages, args)
    filing.setdefault("metadata", {}).update({
        "symbol": args.symbol.upper(), "sector": args.sector,
        "fiscal_year": args.year, "fiscal_period": args.period,
        "source_file": Path(args.pdf).name, "source_sha256": sha,
        "extracted_at": datetime.now(timezone.utc).isoformat(),
        "extractor": {"provider": cfg["name"], "model": cfg["model"]},
    })

    print(f"📊 Extracted: {len(filing.get('statements', []))} statements, "
          f"{len(filing.get('notes', []))} notes, audit={filing.get('audit', {}).get('opinion_type')}")

    problems = validate_filing(filing)
    if problems:
        print(f"⚠️  {len(problems)} contract problem(s):")
        for pr in problems[:25]:
            print(f"   - {pr}")
        print("   (saved for inspection; NOT uploading a non-conforming extract)")

    save_json(filing, args)
    _written, artifacts = write_outputs(filing, args)

    if problems:
        print("❌ Not uploading — fix extraction problems above first.")
        return 2
    if args.dry_run:
        print("📤 --dry-run — saved only, not uploaded.")
        return 0
    if not args.token:
        print("📤 No INGEST_TOKEN set — saved only. Set INGEST_TOKEN to upload to qscreen.app.")
        return 0

    fold = (artifacts or {}).get("analysis") if getattr(args, "with_analysis", False) else None
    print("📤 Uploading to qscreen.app …" + (" (with analysis)" if fold else ""))
    print(f"   ✅ {upload_filing(filing, args, fold)}")
    return 0


def read_manifest(path: str) -> list[dict]:
    """Parse a batch CSV with columns: pdf, symbol, sector, year[, period]."""
    import csv
    rows: list[dict] = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for i, raw in enumerate(csv.DictReader(f), 1):
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
            missing = [c for c in ("pdf", "symbol", "sector", "year") if not row.get(c)]
            if missing:
                raise SystemExit(f"manifest row {i}: missing column(s): {', '.join(missing)}")
            rows.append(row)
    if not rows:
        raise SystemExit(f"manifest {path!r} has no data rows")
    return rows


def run_batch(args) -> int:
    rows = read_manifest(args.manifest)
    print(f"📚 Batch: {len(rows)} filing(s) from {args.manifest}")
    worst, results = 0, []
    for i, row in enumerate(rows, 1):
        period = (row.get("period") or "FY").upper()
        print(f"\n══ [{i}/{len(rows)}] {row['symbol']} {row['year']} {period} ══")
        sector = _normalize_sector(row["sector"])
        if sector not in SECTORS:
            print(f"   ⚠️  unknown sector {row['sector']!r}; using 'other'")
            sector = "other"
        ra = copy.copy(args)
        ra.pdf, ra.symbol, ra.sector, ra.year, ra.period = (
            row["pdf"], row["symbol"], sector, int(row["year"]), period)
        try:
            code = run_filing(ra)
        except SystemExit as e:
            print(f"   ❌ {e}")
            code = 1
        except Exception as e:  # one bad filing must not abort the batch
            print(f"   ❌ {type(e).__name__}: {e}")
            code = 1
        results.append((row["symbol"], row["year"], period, code))
        worst = max(worst, code)
    print("\n── batch summary ──")
    for sym, yr, per, code in results:
        mark = "✅" if code == 0 else ("⚠️ " if code == 2 else "❌")
        print(f"   {mark} {sym} {yr} {per} (exit {code})")
    return worst


def main() -> int:
    p = argparse.ArgumentParser(description="One-file QSE filing ingestor for qscreen.app")
    p.add_argument("--version", action="version", version=f"qscreen-filing-tool {__version__}")
    p.add_argument("pdf", nargs="?", help="Path to the filing PDF")
    p.add_argument("--symbol")
    p.add_argument("--sector", choices=SECTORS)
    p.add_argument("--year", type=int)
    p.add_argument("--period", choices=["FY", "Q1", "Q2", "Q3", "Q4", "H1", "9M"], default="FY")
    p.add_argument("--provider", choices=PROVIDER_CHOICES, default=None,
                   help="minimax | openrouter | kimi | openai | anthropic(=claude) | custom. "
                        "Default: env QSCREEN_PROVIDER, else auto-detected from whichever API key is set.")
    p.add_argument("--base-url", help="Override base URL (required for --provider custom)")
    p.add_argument("--model", help="Override model id (default per provider; or env QSCREEN_MODEL)")
    p.add_argument("--list-providers", action="store_true", help="Show supported providers and exit")
    p.add_argument("--max-tokens", type=int, default=16384)
    p.add_argument("--timeout", type=int, default=600)
    p.add_argument("--retries", type=int, default=4)
    p.add_argument("--pages-per-chunk", type=int, default=12)
    p.add_argument("--overlap", type=int, default=1)
    p.add_argument("--no-chunk", action="store_true")
    p.add_argument("--ocr", choices=["auto", "never", "always"], default="auto",
                   help="OCR scanned pages (auto: only near-empty pages; needs pytesseract+tesseract)")
    p.add_argument("--no-json-mode", action="store_true",
                   help="Don't send response_format=json_object (some providers reject it)")
    p.add_argument("--mode", choices=["auto", "basic", "pro"], default=None,
                   help="basic = deterministic-first, great for tiny/local models; "
                        "pro = the model extracts everything (use a strong model); "
                        "auto (default) = basic for local runtimes, pro for cloud.")
    p.add_argument("--basic", action="store_true", help="Shortcut for --mode basic.")
    p.add_argument("--pro", action="store_true", help="Shortcut for --mode pro.")
    p.add_argument("--no-llm", action="store_true",
                   help="Deterministic only: read line items from the PDF's tables and NEVER call "
                        "the model (audit stays 'unknown', notes stay []). Implies --basic; needs no key.")
    p.add_argument("--guided", action="store_true",
                   help="Alias for --basic (walk a small / local model through the filing in tiny, "
                        "rule-guided steps; auto-on for local runtimes like Ollama).")
    p.add_argument("--no-guided", action="store_true",
                   help="Alias for --pro (force the single big-prompt extractor even on a local provider).")
    p.add_argument("--guided-notes", action="store_true",
                   help="In Basic mode, also do a best-effort pass over the accounting notes.")
    p.add_argument("--export", choices=["csv", "xlsx", "html"], action="append",
                   help="Also write csv (flat line-items table), xlsx (multi-sheet Excel "
                        "transcript), and/or html (printable statements document). Repeatable.")
    p.add_argument("--analyze", action="store_true",
                   help="Also compute and save <symbol>_<year>_<period>_analysis.json + _valuation.json")
    p.add_argument("--with-analysis", action="store_true",
                   help="Fold the derived analysis into the qscreen.app upload payload (additive)")
    p.add_argument("--report", action="store_true",
                   help="Also render the one-page analyst report → <symbol>_<year>_<period>_report.html (+ .md)")
    p.add_argument("--price", type=float, default=None, help="Share price, for the report's valuation upside")
    p.add_argument("--shares", type=float, default=None, help="Shares outstanding, for per-share valuation")
    p.add_argument("--manifest", help="Batch mode: CSV with columns pdf,symbol,sector,year[,period]")
    p.add_argument("--llm-key", default=None,
                   help="API key (else read from the provider's env var, e.g. MINIMAX_API_KEY)")
    p.add_argument("--api-url", default=os.getenv("QSCREEN_API_URL", "http://localhost:3004"))
    p.add_argument("--token", default=os.getenv("INGEST_TOKEN"))
    p.add_argument("--dry-run", action="store_true", help="Extract + save, but do not upload")
    p.add_argument("--self-test", action="store_true", help="Validate contract/normalize/merge offline and exit")
    args = p.parse_args()

    if args.list_providers:
        print(list_providers())
        print("\n" + provider_diagnostic())
        return 0
    if args.self_test:
        return run_self_test()

    if args.manifest:
        return run_batch(args)

    missing = [n for n in ("pdf", "symbol", "sector", "year") if not getattr(args, n)]
    if missing:
        p.error(f"missing required argument(s): {', '.join('--' + m if m != 'pdf' else 'pdf' for m in missing)}")
    return run_filing(args)


if __name__ == "__main__":
    sys.exit(main())
