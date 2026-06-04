#!/usr/bin/env python3
"""qscreen_charts.py — tiny dependency-free SVG charts for the analyst report.

Pure-Python SVG strings (no matplotlib, no JS) so the report stays a single
self-contained, offline HTML file. Two primitives:

    sparkline(values)            -> a small inline trend line (for table rows)
    bars(labels, values, title)  -> a labelled vertical bar chart

Both tolerate None/empty gracefully (return "" when there is nothing to draw),
put the baseline at zero, and colour negative bars differently.
"""
from __future__ import annotations

import html


def _finite(values) -> list:
    return [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]


def _compact(v) -> str:
    a = abs(v)
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if a >= div:
            return f"{v / div:.1f}{suf}".replace(".0", "")
    return f"{v:.0f}" if a >= 1 else f"{v:.2f}"


def sparkline(values, width: int = 86, height: int = 20, stroke: str = "#36c") -> str:
    pts = [(i, v) for i, v in enumerate(values) if isinstance(v, (int, float)) and not isinstance(v, bool)]
    if len(pts) < 2:
        return ""
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    vmin, vmax = min(ys), max(ys)
    span = (vmax - vmin) or 1
    pad = 2
    xspan = (xs[-1] - xs[0]) or 1

    def X(i):
        return pad + (i - xs[0]) / xspan * (width - 2 * pad)

    def Y(v):
        return height - pad - (v - vmin) / span * (height - 2 * pad)

    poly = " ".join(f"{X(i):.1f},{Y(v):.1f}" for i, v in pts)
    lx, lv = pts[-1]
    return (f"<svg class='spark' width='{width}' height='{height}' viewBox='0 0 {width} {height}'>"
            f"<polyline fill='none' stroke='{stroke}' stroke-width='1.5' points='{poly}'/>"
            f"<circle cx='{X(lx):.1f}' cy='{Y(lv):.1f}' r='1.8' fill='{stroke}'/></svg>")


def bars(labels, values, width: int = 460, height: int = 190, title: str | None = None,
         color: str = "#36c", neg_color: str = "#c33") -> str:
    fin = _finite(values)
    if not fin:
        return ""
    vmax = max(fin + [0])
    vmin = min(fin + [0])
    span = (vmax - vmin) or 1
    n = len(values)
    pad_l, pad_b = 8, 18
    pad_t = 20 if title else 8
    plot_h = height - pad_b - pad_t
    gap = (width - 2 * pad_l) / n
    bw = gap * 0.62

    def Y(v):
        return pad_t + (vmax - v) / span * plot_h

    y0 = Y(0)
    out = [f"<svg class='bars' width='{width}' height='{height}' viewBox='0 0 {width} {height}'>"]
    if title:
        out.append(f"<text x='{pad_l}' y='13' font-size='11' fill='#555' font-weight='600'>"
                   f"{html.escape(str(title))}</text>")
    out.append(f"<line x1='{pad_l}' y1='{y0:.1f}' x2='{width - pad_l}' y2='{y0:.1f}' stroke='#ccc'/>")
    for i, v in enumerate(values):
        cx = pad_l + gap * i + (gap - bw) / 2
        mid = cx + bw / 2
        label = html.escape(str(labels[i])) if i < len(labels) else ""
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            top = Y(max(v, 0))
            h = abs(Y(v) - y0)
            fill = color if v >= 0 else neg_color
            out.append(f"<rect class='bar' x='{cx:.1f}' y='{top:.1f}' width='{bw:.1f}' "
                       f"height='{h:.1f}' fill='{fill}' rx='1'/>")
            ly = top - 3 if v >= 0 else Y(v) + 10
            out.append(f"<text x='{mid:.1f}' y='{ly:.1f}' text-anchor='middle' "
                       f"font-size='9' fill='#555'>{_compact(v)}</text>")
        out.append(f"<text x='{mid:.1f}' y='{height - 5}' text-anchor='middle' "
                   f"font-size='9' fill='#888'>{label}</text>")
    out.append("</svg>")
    return "".join(out)
