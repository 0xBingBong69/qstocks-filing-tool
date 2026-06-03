"""Tests for period-aware TTM / quarterly roll-ups."""
from __future__ import annotations

import qscreen_periods as qp


def _f(year, period, ni, nii, ta=None, eq=None):
    lis = [{"account_code": "IS_NET_INCOME", "label_verbatim": "NI", "value": ni},
           {"account_code": "IS_NET_INTEREST", "label_verbatim": "NII", "value": nii}]
    if ta is not None:
        lis.append({"account_code": "BS_TOTAL_ASSETS", "label_verbatim": "TA", "value": ta})
    if eq is not None:
        lis.append({"account_code": "BS_TOTAL_EQUITY", "label_verbatim": "EQ", "value": eq})
    return {"metadata": {"symbol": "QNBK", "fiscal_year": year, "fiscal_period": period},
            "statements": [{"type": "income_statement", "line_items": lis}]}


def test_period_months_map():
    assert qp.PERIOD_MONTHS["9M"] == 9 and qp.PERIOD_MONTHS["H1"] == 6 and qp.PERIOD_MONTHS["FY"] == 12


def test_true_ttm_from_interim_plus_priors():
    t = qp.build_ttm([_f(2023, "9M", 10500, 18000), _f(2023, "FY", 14000, 24000, ta=1_150_000, eq=100_000),
                      _f(2024, "H1", 7800, 13000), _f(2024, "9M", 12000, 20000, ta=1_200_000, eq=105_000)])
    assert t["as_of"] == "9M 2024"
    assert t["basis"] == "TTM = 9M 2024 + FY 2023 − 9M 2023"
    assert t["flows"]["IS_NET_INCOME"] == 15500          # 12000 + 14000 − 10500
    assert t["flows"]["IS_NET_INTEREST"] == 26000        # 20000 + 24000 − 18000
    assert t["ttm_months"] == 12


def test_stocks_are_latest_point_in_time():
    t = qp.build_ttm([_f(2023, "FY", 14000, 24000, ta=1_150_000, eq=100_000),
                      _f(2024, "9M", 12000, 20000, ta=1_200_000, eq=105_000),
                      _f(2023, "9M", 10500, 18000), _f(2024, "H1", 7800, 13000)])
    assert t["stocks"] == {"BS_TOTAL_ASSETS": 1_200_000, "BS_TOTAL_EQUITY": 105_000}


def test_standalone_quarter_is_ytd_delta():
    t = qp.build_ttm([_f(2024, "H1", 7800, 13000), _f(2024, "9M", 12000, 20000),
                      _f(2023, "FY", 14000, 24000), _f(2023, "9M", 10500, 18000)])
    sq = t["standalone_quarter"]
    assert sq["label"] == "9M 2024 standalone (3m)"
    assert sq["flows"]["IS_NET_INCOME"] == 4200          # 12000 − 7800
    assert sq["flows"]["IS_NET_INTEREST"] == 7000        # 20000 − 13000


def test_annual_only_reports_fy_as_is():
    t = qp.build_ttm([_f(2023, "FY", 14000, 24000, ta=1_150_000)])
    assert t["basis"] == "FY 2023 as reported"
    assert t["flows"]["IS_NET_INCOME"] == 14000 and not t["warnings"]
    assert t["standalone_quarter"] is None               # no interim → no standalone


def test_interim_without_priors_warns_and_falls_back():
    t = qp.build_ttm([_f(2024, "9M", 12000, 20000)])
    assert t["ttm_months"] == 9 and t["warnings"]
    assert "true TTM" in t["basis"] and t["flows"]["IS_NET_INCOME"] == 12000


def test_q1_standalone_is_itself():
    t = qp.build_ttm([_f(2024, "Q1", 3000, 5000)])
    assert t["standalone_quarter"]["flows"]["IS_NET_INCOME"] == 3000


def test_empty_input():
    t = qp.build_ttm([])
    assert t["flows"] == {} and t["warnings"]
