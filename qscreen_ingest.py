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

DEPS:  pip install pdfplumber requests

The tool: PDF -> (pdfplumber text + recovered tables) -> overlapping page
windows -> LLM (minimax/openrouter/kimi/openai/anthropic) -> normalized +
merged lossless JSON -> validated against the contract -> saved AND uploaded.
Self-test: --self-test.   Providers: --list-providers.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

__version__ = "1.0.0"


# ── .env loader (no python-dotenv dependency) ────────────────────────────────

def _load_dotenv() -> None:
    here = Path(__file__).resolve().parent
    for candidate in (here / ".env", here.parent / ".env"):
        if not candidate.is_file():
            continue
        for raw in candidate.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip().strip('"').strip("'")
            if key and key not in os.environ:
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
}
# Friendly aliases the user can type for --provider / QSCREEN_PROVIDER.
PROVIDER_ALIASES = {"claude": "anthropic", "moonshot": "kimi", "gpt": "openai", "oai": "openai"}
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
    rows = ["Providers — put the matching API key in .env (the tool auto-detects it):", ""]
    for name, p in PROVIDERS.items():
        rows.append(f"  {name:11s} {p['label']:18s} {p['env'][0]:20s} model={p['default_model']}")
        rows.append(f"  {'':11s} └─ get a key:  {p['key_url']}")
    rows.append(f"  {'custom':11s} {'(any OpenAI URL)':18s} {'LLM_API_KEY':20s} pass --base-url --model")
    rows.append("")
    rows.append("Aliases: " + ", ".join(f"{a}→{b}" for a, b in PROVIDER_ALIASES.items()))
    rows.append("Force one with --provider NAME (or env QSCREEN_PROVIDER); pick a model with "
                "--model (or env QSCREEN_MODEL).")
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


def resolve_provider(args) -> dict:
    """Turn the chosen provider + overrides + env into a concrete
    {name, base_url, kind, model, key}. Raises SystemExit with guidance if the
    provider or key can't be determined — no network is touched."""
    name = canonical_provider(getattr(args, "provider", None)) or detect_provider()
    if not name:
        raise SystemExit("No LLM provider selected and no provider API key found.\n\n" + list_providers())

    if name == "custom":
        base = getattr(args, "base_url", None)
        if not base:
            raise SystemExit("--provider custom requires --base-url (any OpenAI-compatible endpoint).")
        cfg = {"base_url": base, "kind": "openai", "env": ("LLM_API_KEY",), "default_model": None}
    else:
        cfg = PROVIDERS.get(name)
        if not cfg:
            raise SystemExit(f"Unknown provider {name!r}.\n\n" + list_providers())
        cfg = dict(cfg)
        if getattr(args, "base_url", None):
            cfg["base_url"] = args.base_url

    model = getattr(args, "model", None) or os.getenv("QSCREEN_MODEL") or cfg["default_model"]
    if not model:
        raise SystemExit(f"No model for provider {name!r}; pass --model or set QSCREEN_MODEL.")
    key = (getattr(args, "llm_key", None)
           or next((os.getenv(k) for k in cfg["env"] if os.getenv(k)), None)
           or os.getenv("LLM_API_KEY"))
    if not key:
        want = " or ".join(cfg["env"]) + " (or LLM_API_KEY)"
        raise SystemExit(f"No API key for provider {name!r}. Set {want} in .env or pass --llm-key.")
    return {"name": name, "base_url": cfg["base_url"].rstrip("/"),
            "kind": cfg["kind"], "model": model, "key": key}


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

def _openai_request(messages: list[dict], cfg: dict, args):
    url = f"{cfg['base_url']}/chat/completions"
    payload = {"model": cfg["model"], "messages": messages,
               "temperature": 0, "max_tokens": args.max_tokens}
    if not getattr(args, "no_json_mode", False):
        payload["response_format"] = {"type": "json_object"}
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


# ── Orchestration ─────────────────────────────────────────────────────────────

def extract_filing(pages: list[dict], args) -> dict:
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


def export_xlsx(filing: dict, path: str) -> int:
    try:
        from openpyxl import Workbook
    except Exception:
        raise SystemExit("xlsx export needs openpyxl:  pip install openpyxl")
    rows = flatten_line_items(filing)
    wb = Workbook()
    ws = wb.active
    ws.title = "line_items"
    ws.append(EXPORT_COLUMNS)
    for r in rows:
        ws.append([r[c] for c in EXPORT_COLUMNS])
    wb.save(path)
    return len(rows)


# ── Main ──────────────────────────────────────────────────────────────────────

def save_json(filing: dict, args) -> str:
    out = f"{args.symbol.upper()}_{args.year}_{args.period}_filing.json"
    Path(out).write_text(json.dumps(filing, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"💾 Saved qscreen-uploadable file → {out}")
    return out


def run_filing(args) -> int:
    """Extract one PDF → save (+ optional export) → optionally upload. Returns
    an exit code: 0 ok, 2 saved-but-non-conforming (not uploaded)."""
    cfg = resolve_provider(args)        # fail fast on bad provider/key before any work
    if qatar is not None and getattr(args, "symbol", None):
        args._profile = qatar.profile_for_year(args.symbol, getattr(args, "year", None))
        if args._profile:
            print(f"🇶🇦 Qatar profile: {args._profile.get('name_as_of')} "
                  f"[{args._profile.get('archetype')}] — "
                  f"{len(args._profile.get('active_events') or [])} regime/event(s) in force by {args.year}")
    print(f"📄 Reading {Path(args.pdf).name} …  (provider: {cfg['name']}, model: {cfg['model']})")
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
    for fmt in (getattr(args, "export", None) or []):
        out = f"{args.symbol.upper()}_{args.year}_{args.period}_filing.{fmt}"
        n = export_csv(filing, out) if fmt == "csv" else export_xlsx(filing, out)
        print(f"📑 Exported {n} line item(s) → {out}")

    # Both outputs: optionally also persist the derived analysis/valuation locally.
    # A failure here must never sink a successful extraction — but it IS surfaced.
    artifacts = None
    if getattr(args, "analyze", False) or getattr(args, "with_analysis", False):
        try:
            artifacts = build_analysis_artifacts(filing, args)
        except Exception as e:
            print(f"   ⚠️  analysis step failed (extraction is unaffected): {e}")
    if getattr(args, "analyze", False) and artifacts:
        base = f"{args.symbol.upper()}_{args.year}_{args.period}"
        if artifacts.get("analysis"):
            Path(f"{base}_analysis.json").write_text(
                json.dumps(artifacts["analysis"], indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"🧮 Saved analysis → {base}_analysis.json "
                  f"({len(artifacts['analysis'].get('red_flags', []))} red flag(s))")
        if (artifacts.get("valuation") or {}).get("valuation"):
            Path(f"{base}_valuation.json").write_text(
                json.dumps(artifacts["valuation"], indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"💰 Saved valuation → {base}_valuation.json "
                  f"({artifacts['valuation']['valuation']['model']})")

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
    p.add_argument("--export", choices=["csv", "xlsx"], action="append",
                   help="Also write a flattened line-items table (repeatable: --export csv --export xlsx)")
    p.add_argument("--analyze", action="store_true",
                   help="Also compute and save <symbol>_<year>_<period>_analysis.json + _valuation.json")
    p.add_argument("--with-analysis", action="store_true",
                   help="Fold the derived analysis into the qscreen.app upload payload (additive)")
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
