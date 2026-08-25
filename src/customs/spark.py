"""Grafana's numbers, drawn here.

A chart inside a card cannot be an iframe: Grafana Cloud answers with
frame-ancestors 'none'. It should not be a server-rendered PNG either --
a PNG is a fixed size, a fixed theme and a network round trip, and a card
has to be small, sharp, instant and follow whichever style mode the
operator picked.

So the split is: Grafana keeps the data, the app keeps the drawing. Mimir
and Loki remain the single source of truth, the MCP agent queries exactly
the same series, and the card renders inline SVG in the product's own hex.
Nothing is embedded, so nothing can be blocked or mis-themed.

The four colours are the ones the whole product uses. They live in
customs/state.py so a threshold can never mean one colour in the app and
another in a Grafana panel.
"""
from __future__ import annotations

from customs.state import BLOCKED, CLEARED, AT_RISK, colour_for_severity


def _path(points: list[tuple[float, float]], width: float, height: float,
          floor: float, ceiling: float) -> tuple[str, str]:
    """A line and its matching filled area, in SVG user units."""
    if not points:
        return "", ""
    span = max(1e-6, points[-1][0] - points[0][0])
    reach = max(1e-6, ceiling - floor)
    xs, ys = [], []
    for ts, value in points:
        xs.append((ts - points[0][0]) / span * width)
        ys.append(height - (min(max(value, floor), ceiling) - floor) / reach * height)
    line = "M" + " L".join(f"{x:.2f},{y:.2f}" for x, y in zip(xs, ys))
    area = f"{line} L{xs[-1]:.2f},{height:.2f} L{xs[0]:.2f},{height:.2f} Z"
    return line, area


def sparkline(points: list[tuple[float, float]], *, width: int = 260,
              height: int = 40, ceiling: float = 100.0) -> str:
    """One severity profile as inline SVG, coloured by its own peak.

    Reads as "where in this film is the risk" rather than as a number: the
    shape is the point, so there are no axes, no grid and no legend.
    """
    if not points:
        return ""
    peak = max(v for _, v in points)
    colour = colour_for_severity(peak)
    line, area = _path(points, width, height, 0.0, ceiling)
    ident = f"sg{abs(hash((len(points), round(peak, 2)))) % 100000}"
    return (
        f'<svg class="spark" viewBox="0 0 {width} {height}" '
        f'preserveAspectRatio="none" aria-hidden="true">'
        f'<defs><linearGradient id="{ident}" x1="0" x2="0" y1="0" y2="1">'
        f'<stop offset="0" stop-color="{colour}" stop-opacity=".26"/>'
        f'<stop offset="1" stop-color="{colour}" stop-opacity="0"/>'
        f'</linearGradient></defs>'
        f'<path d="{area}" fill="url(#{ident})"/>'
        f'<path d="{line}" fill="none" stroke="{colour}" stroke-width="2" '
        f'vector-effect="non-scaling-stroke" stroke-linejoin="round"/>'
        f'</svg>'
    )


def bars(values: list[tuple[str, float]], *, width: int = 260, height: int = 40,
         palette: dict[str, str] | None = None) -> str:
    """A tiny categorical bar row: dimensions, markets, whatever is counted."""
    if not values:
        return ""
    top = max(v for _, v in values) or 1.0
    gap, n = 3, len(values)
    bw = max(2.0, (width - gap * (n - 1)) / n)
    out = []
    for i, (name, value) in enumerate(values):
        h = max(2.0, value / top * height)
        colour = (palette or {}).get(name, CLEARED if value <= 0 else AT_RISK)
        out.append(f'<rect x="{i * (bw + gap):.2f}" y="{height - h:.2f}" '
                   f'width="{bw:.2f}" height="{h:.2f}" rx="1.5" fill="{colour}">'
                   f'<title>{name}: {value:g}</title></rect>')
    return (f'<svg class="spark" viewBox="0 0 {width} {height}" '
            f'preserveAspectRatio="none" aria-hidden="true">{"".join(out)}</svg>')
