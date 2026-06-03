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
    OPENROUTER_API_KEY=sk-or-...        # LLM key (or MINIMAX_API_KEY / LLM_API_KEY)
    INGEST_TOKEN=...                    # qscreen.app ingest token (needed to upload)
    QSCREEN_API_URL=https://qscreen.app # defaults to http://localhost:3004

DEPS:  pip install pdfplumber requests

The tool: PDF -> (pdfplumber text + recovered tables) -> overlapping page
windows -> LLM (OpenRouter/MiniMax) -> normalized + merged lossless JSON ->
validated against the contract -> saved AND uploaded. Self-test: --self-test.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


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

PROVIDER_BASE_URLS = {
    "openrouter": "https://openrouter.ai/api/v1",
    "minimax": "https://api.minimax.io/v1",
    "kimi": "https://api.moonshot.cn/v1",
}
DEFAULT_MODELS = {
    "openrouter": "minimax/minimax-m2.7",
    "minimax": "MiniMax-M2",
    "kimi": "moonshot-v1-128k",
}


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

def _system_prompt(sector: str, windowed: bool) -> str:
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
results of associates).

OUTPUT CONTRACT — emit ONE JSON object with EXACTLY these top-level keys:
  metadata, audit, statements, notes, extraction_quality
Use these EXACT field names (do not rename):
  metadata: {{symbol, company_name, sector, fiscal_year, fiscal_period, \
period_end, currency, unit_scale, reporting_framework, consolidated, language}}
  audit: {{opinion_type, auditor_name, report_date, emphasis_of_matter (array \
of strings), key_audit_matters (array of {{title, text}}), \
material_uncertainty_going_concern ({{present, text}} or null), verbatim_text}}
  statements[]: {{type, title, period_label, verbatim_text, line_items[]}}
  line_items[]: {{account_code, label_verbatim, value, note_ref, depth, is_subtotal}}
  notes[]: {{number, title, category, structured, verbatim_text}}

RULES (in priority order):
1. LOSSLESS VERBATIM. For every statement and every note, copy the COMPLETE \
original text into `verbatim_text`. Never summarize, truncate, or paraphrase. \
This is the most important rule.
2. STRUCTURED TOO. For each printed statement row emit a line_item. `value` is \
the number exactly as printed (do NOT rescale); put the multiplier in \
metadata.unit_scale (1, 1000, or 1000000) from the "in thousands/millions" \
header. Use negative values for amounts printed in brackets.
   - account_code MUST be one of these canonical codes, or null if no clean \
match (when null, still keep label_verbatim): {codes}
   - statements[].type MUST be one of: {statement_types}
3. AUDIT. audit.opinion_type MUST be one of: {opinions}. Put the full opinion \
wording in audit.verbatim_text (NOT a field called opinion_text). Each key \
audit matter is {{title, text}} — no other keys.
4. NOTES. Every note -> one entry with number, title, category (one of: \
{note_cats}), a category-specific `structured` object, and COMPLETE \
verbatim_text. Watch for: contingent liabilities, commitments, other \
comprehensive income, cost/expense breakdowns, related-party, ECL/staged \
provisions, and Islamic profit-sharing/sukuk/quasi-equity.
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
        {"role": "system", "content": _system_prompt(args.sector, windowed)},
        {"role": "user", "content": user},
    ]


# ── LLM call ──────────────────────────────────────────────────────────────────

def call_llm(messages: list[dict], args) -> str:
    import requests
    base = args.base_url or PROVIDER_BASE_URLS.get(args.provider)
    if not base:
        raise SystemExit(f"No base URL for provider {args.provider!r}; pass --base-url")
    model = args.model or DEFAULT_MODELS.get(args.provider) or args.provider
    url = f"{base.rstrip('/')}/chat/completions"
    payload = {"model": model, "messages": messages, "temperature": 0, "max_tokens": args.max_tokens}
    if not args.no_json_mode:
        payload["response_format"] = {"type": "json_object"}
    headers = {"Authorization": f"Bearer {args.llm_key}", "Content-Type": "application/json"}

    last_err = None
    for attempt in range(1, args.retries + 1):
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=args.timeout)
        except requests.RequestException as e:
            last_err = str(e)
        else:
            if resp.status_code in (429, 500, 502, 503, 504):
                last_err = f"HTTP {resp.status_code}: {resp.text[:200]}"  # transient → retry
            elif resp.status_code in (400, 401, 403, 404, 422):
                detail = resp.text[:300]
                hint = ""
                if resp.status_code in (401, 403):
                    hint = (" — check the API key, or this network may be blocking the "
                            "provider (the remote-environment network policy must allow "
                            f"{base}).")
                elif "model" in detail.lower():
                    hint = f" — model {model!r} may be invalid; pass --model with a valid slug."
                raise SystemExit(f"LLM provider error HTTP {resp.status_code}: {detail}{hint}")
            else:
                resp.raise_for_status()
                return resp.json()["choices"][0]["message"]["content"]
        if attempt < args.retries:
            wait = 2 ** attempt
            print(f"   ⚠️  LLM call failed ({last_err}); retry {attempt}/{args.retries - 1} in {wait}s")
            time.sleep(wait)
    raise SystemExit(f"LLM call failed after {args.retries} attempts: {last_err}")


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

def upload_filing(filing: dict, args) -> dict:
    import requests
    url = f"{args.api_url.rstrip('/')}/api/v1/ingest/filing"
    headers = {"Authorization": f"Bearer {args.token}", "Content-Type": "application/json"}
    resp = requests.post(url, headers=headers, json=filing, timeout=180)
    resp.raise_for_status()
    return resp.json()


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


# ── Main ──────────────────────────────────────────────────────────────────────

def save_json(filing: dict, args) -> str:
    out = f"{args.symbol.upper()}_{args.year}_{args.period}_filing.json"
    Path(out).write_text(json.dumps(filing, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"💾 Saved qscreen-uploadable file → {out}")
    return out


def main() -> int:
    p = argparse.ArgumentParser(description="One-file QSE filing ingestor for qscreen.app")
    p.add_argument("pdf", nargs="?", help="Path to the filing PDF")
    p.add_argument("--symbol")
    p.add_argument("--sector", choices=SECTORS)
    p.add_argument("--year", type=int)
    p.add_argument("--period", choices=["FY", "Q1", "Q2", "Q3", "Q4", "H1", "9M"], default="FY")
    p.add_argument("--provider", choices=["openrouter", "minimax", "kimi", "custom"], default="openrouter")
    p.add_argument("--base-url", help="Override base URL (required for --provider custom)")
    p.add_argument("--model", help="Override model id (default per provider)")
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
    p.add_argument("--llm-key", default=(os.getenv("LLM_API_KEY") or os.getenv("OPENROUTER_API_KEY")
                                         or os.getenv("MINIMAX_API_KEY")))
    p.add_argument("--api-url", default=os.getenv("QSCREEN_API_URL", "http://localhost:3004"))
    p.add_argument("--token", default=os.getenv("INGEST_TOKEN"))
    p.add_argument("--dry-run", action="store_true", help="Extract + save, but do not upload")
    p.add_argument("--self-test", action="store_true", help="Validate contract/normalize/merge offline and exit")
    args = p.parse_args()

    if args.self_test:
        return run_self_test()

    missing = [n for n in ("pdf", "symbol", "sector", "year") if not getattr(args, n)]
    if missing:
        p.error(f"missing required argument(s): {', '.join('--' + m if m != 'pdf' else 'pdf' for m in missing)}")
    if not args.llm_key:
        p.error("LLM key required (set OPENROUTER_API_KEY in .env, or pass --llm-key)")

    print(f"📄 Reading {Path(args.pdf).name} …")
    pages, sha = pdf_to_pages(args.pdf, args.ocr)
    total_chars = sum(len(pg["text"]) for pg in pages)
    print(f"   {len(pages)} pages, {total_chars:,} chars (text + recovered tables), sha256={sha[:12]}…")

    filing = extract_filing(pages, args)
    filing.setdefault("metadata", {}).update({
        "symbol": args.symbol.upper(), "sector": args.sector,
        "fiscal_year": args.year, "fiscal_period": args.period,
        "source_file": Path(args.pdf).name, "source_sha256": sha,
        "extracted_at": datetime.now(timezone.utc).isoformat(),
        "extractor": {"provider": args.provider, "model": args.model or DEFAULT_MODELS.get(args.provider)},
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

    if problems:
        print("❌ Not uploading — fix extraction problems above first.")
        return 2
    if args.dry_run:
        print("📤 --dry-run — saved only, not uploaded.")
        return 0
    if not args.token:
        print("📤 No INGEST_TOKEN set — saved only. Set INGEST_TOKEN to upload to qscreen.app.")
        return 0

    print("📤 Uploading to qscreen.app …")
    print(f"   ✅ {upload_filing(filing, args)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
