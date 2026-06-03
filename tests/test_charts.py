"""Tests for the dependency-free SVG charts."""
from __future__ import annotations

import qscreen_charts as ch


def test_compact_formatting():
    assert ch._compact(1.23e9) == "1.2B"
    assert ch._compact(8.3e5) == "830k"
    assert ch._compact(-1.5e6) == "-1.5M"
    assert ch._compact(42) == "42"


def test_sparkline_needs_two_points():
    assert ch.sparkline([5]) == "" and ch.sparkline([None, None]) == ""
    sp = ch.sparkline([10, 12, 11, 14, 15])
    assert sp.startswith("<svg") and sp.endswith("</svg>")
    assert "<polyline" in sp and sp.count(",") >= 4          # one x,y per point


def test_sparkline_skips_none_points():
    sp = ch.sparkline([10, None, 14])                        # 2 finite points → still drawn
    assert "<polyline" in sp


def test_bars_counts_and_colours():
    b = ch.bars(["2021", "2022", "2023"], [14000, -2000, 15500], title="Net income")
    assert b.count("class='bar'") == 3                       # one rect per finite value
    assert "#c33" in b and "#36c" in b                       # negative vs positive colour
    assert "<line" in b and "Net income" in b                # zero baseline + title
    assert b.startswith("<svg") and b.endswith("</svg>")     # self-contained


def test_bars_empty_when_nothing_finite():
    assert ch.bars(["a", "b"], [None, None]) == ""
    assert ch.bars([], []) == ""


def test_bars_skips_none_but_keeps_axis_label():
    b = ch.bars(["2021", "2022"], [None, 100])
    assert b.count("class='bar'") == 1 and ">2021<" in b     # missing bar, label still shown
