"""Tests for the watchlist screener (multi-stock roll-up + ranking)."""
from __future__ import annotations

import qatar
import qscreen_portfolio as pf


def _filing(sym, ni, eq, prev_eq, car=None):
    li = [{"account_code": "IS_NET_INCOME", "label_verbatim": "x", "value": ni,
           "comparatives": [{"period_label": "2022", "value": ni * 0.9}]},
          {"account_code": "BS_TOTAL_EQUITY", "label_verbatim": "x", "value": eq,
           "comparatives": [{"period_label": "2022", "value": prev_eq}]}]
    if car is not None:
        li.append({"account_code": "KPI_CAR", "label_verbatim": "x", "value": car,
                   "comparatives": [{"period_label": "2022", "value": car}]})
    return {"metadata": {"symbol": sym, "fiscal_year": 2023, "fiscal_period": "FY", "currency": "QAR"},
            "statements": [{"type": "income_statement", "verbatim_text": "x", "line_items": li}]}


def _board():
    groups = {"QNBK": [_filing("QNBK", 15000, 100000, 95000, 19.5)],   # healthy, ROE 15%
              "DHBK": [_filing("DHBK", 1200, 90000, 100000, 12.0)],    # low CAR + equity decline → alert
              "QIBK": [_filing("QIBK", 4000, 40000, 38000)]}           # ROE 10%
    profs = {s: qatar.profile_for_year(s, 2023) for s in groups}
    return pf.roll_up(groups, profs)


def test_roll_up_ranks_healthiest_first():
    board = _board()
    assert board["count"] == 3
    assert board["rows"][0]["symbol"] == "QNBK"           # 0 alerts, highest ROE
    assert board["rows"][-1]["symbol"] == "DHBK"          # low-CAR alert sinks it
    assert board["rows"][-1]["alerts"] >= 1


def test_roll_up_row_fields():
    row = next(r for r in _board()["rows"] if r["symbol"] == "QNBK")
    assert round(row["roe"] * 100) == 15 and row["archetype"] == "conventional_bank"
    assert "per_share" in row and "ni_growth" in row


def test_render_html_dashboard():
    h = pf.render_html(_board())
    assert h.startswith("<!doctype html>")
    for s in ["QSE watchlist", "QNBK", "DHBK", "ROE", "Flags", "Upside"]:
        assert s in h
