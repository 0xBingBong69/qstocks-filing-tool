"""Qatar (QSE) per-stock knowledge base — the tool's pre-loaded analyst memory.

Public API:
    all_tickers()                  -> sorted list of the 55 QSE tickers
    load_profile("QNBK")           -> full static profile (or None if unknown)
    profile_for_year("QNBK", 2015) -> profile resolved AS OF a fiscal year
                                      (name, framework, active subsidiaries/
                                       currencies, and the events in force)
    taxonomy() / symbol_subsector() / subsector_to_archetype()
    export_json(dir)               -> write qatar/profiles/<TICKER>.json for all 55

A profile is built from qatar/_seed.py: a base derived from the QSE taxonomy
(name, sub-sector, archetype, framework, watch-KPIs) plus hand-authored temporal
enrichment (name changes, acquisitions, regulatory regimes, expected segments).
"""
from __future__ import annotations

import json
from pathlib import Path

from . import _seed
from ._seed import (
    QSE_TAXONOMY, SYMBOL_SUBSECTOR, SUBSECTOR_TO_ARCHETYPE,
    WATCH_KPIS, COMPANY_NAMES, ENRICH,
)

# Name kept for back-compat with qscreen_app.py's existing variable.
SUBSECTOR_TO_EXTRACTION = SUBSECTOR_TO_ARCHETYPE

_PROFILES_DIR = Path(__file__).resolve().parent / "profiles"


def _default_framework(archetype: str) -> list[dict]:
    fw = "IFRS as adopted by QCB (Islamic)" if archetype == "islamic_bank" else "IFRS"
    return [{"framework": fw, "from": None}]


def _default_peers(ticker: str, sub_sector: str) -> list[str]:
    same = [t for t, s in SYMBOL_SUBSECTOR.items() if s == sub_sector and t != ticker]
    return same[:4]


def build_profile(ticker: str) -> dict:
    """Construct the full static profile for one ticker from seed + enrichment."""
    sub = SYMBOL_SUBSECTOR[ticker]
    archetype = SUBSECTOR_TO_ARCHETYPE[sub]
    enrich = ENRICH.get(ticker, {})
    name = enrich.get("company_name") or COMPANY_NAMES.get(ticker) or ticker
    # Banks inherit the Qatar-wide regulatory timeline (Basel III, IFRS 9).
    bank_events = []
    if archetype in ("conventional_bank", "islamic_bank"):
        bank_events = [dict(_seed._BASEL_III), dict(_seed._IFRS9)]
    events = sorted(bank_events + list(enrich.get("events", [])),
                    key=lambda e: (e.get("year") or 0))
    return {
        "ticker": ticker,
        "company_name": name,
        "names": enrich.get("names") or [{"name": name, "from": None, "to": None}],
        "sub_sector": sub,
        "archetype": archetype,
        "fiscal_year_end": enrich.get("fiscal_year_end", "12-31"),
        "reporting_currency": enrich.get("reporting_currency", "QAR"),
        "framework_timeline": enrich.get("framework_timeline") or _default_framework(archetype),
        "watch_kpis": enrich.get("watch_kpis") or list(WATCH_KPIS.get(archetype, WATCH_KPIS["other"])),
        "segments_expected": enrich.get("segments_expected") or {"by_geography": [], "by_business": []},
        "subsidiaries": enrich.get("subsidiaries", []),
        "events": events,
        "peers": enrich.get("peers") or _default_peers(ticker, sub),
        "accounting_quirks": enrich.get("accounting_quirks", []),
    }


# Build all 55 once at import (cheap, pure-Python).
_PROFILES: dict[str, dict] = {t: build_profile(t) for t in SYMBOL_SUBSECTOR}


def _norm(ticker: str | None) -> str:
    return (ticker or "").strip().upper().replace(".QA", "")


def all_tickers() -> list[str]:
    return sorted(_PROFILES)


def load_profile(ticker: str) -> dict | None:
    """Full static profile, or None if the ticker isn't a known QSE listing."""
    return _PROFILES.get(_norm(ticker))


def profile_for_year(ticker: str, year: int | None) -> dict | None:
    """Profile resolved as of a fiscal year: name, framework, active
    subsidiaries/currencies, and the events already in force by that year."""
    p = load_profile(ticker)
    if p is None:
        return None
    if year is None:
        return dict(p, as_of_year=None, name_as_of=p["company_name"],
                    framework_as_of=p["framework_timeline"][0]["framework"],
                    active_subsidiaries=list(p["subsidiaries"]),
                    active_currencies=sorted({s["currency"] for s in p["subsidiaries"]
                                              if s.get("currency")}),
                    active_events=list(p["events"]))

    name = p["company_name"]
    for n in p["names"]:
        frm, to = n.get("from"), n.get("to")
        if (frm is None or year >= frm) and (to is None or year <= to):
            name = n["name"]

    framework = p["framework_timeline"][0]["framework"]
    for fr in sorted(p["framework_timeline"], key=lambda x: (x.get("from") or 0)):
        if fr.get("from") is None or year >= fr["from"]:
            framework = fr["framework"]

    active_subs = [s for s in p["subsidiaries"]
                   if (s.get("from") is None or year >= s["from"])
                   and (s.get("to") is None or year <= s["to"])]
    active_events = [e for e in p["events"] if (e.get("year") is None or year >= e["year"])]
    return dict(
        p, as_of_year=year, name_as_of=name, framework_as_of=framework,
        active_subsidiaries=active_subs,
        active_currencies=sorted({s["currency"] for s in active_subs if s.get("currency")}),
        active_events=active_events,
    )


def taxonomy() -> dict:
    return QSE_TAXONOMY


def symbol_subsector() -> dict:
    return SYMBOL_SUBSECTOR


def subsector_to_archetype() -> dict:
    return SUBSECTOR_TO_ARCHETYPE


def export_json(directory: str | Path | None = None) -> int:
    """Write qatar/profiles/<TICKER>.json for all 55 (inspectable artifacts)."""
    out = Path(directory) if directory else _PROFILES_DIR
    out.mkdir(parents=True, exist_ok=True)
    for ticker, profile in _PROFILES.items():
        (out / f"{ticker}.json").write_text(
            json.dumps(profile, indent=2, ensure_ascii=False), encoding="utf-8")
    return len(_PROFILES)
