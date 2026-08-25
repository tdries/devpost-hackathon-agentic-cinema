"""Grafana's numbers, drawn here.

A chart inside a card cannot be an iframe: Grafana Cloud answers with
x-frame-options: deny. It should not be a server-rendered PNG either --
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
        f'<svg xmlns="http://www.w3.org/2000/svg" class="spark" '
        f'width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
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


def statcard(points: list[tuple[float, float]], *, value: str, label: str,
             colour: str | None = None, width: int = 260, height: int = 76) -> str:
    """A Grafana stat panel, drawn here: the number, with its trend behind it.

    The first version of this was a bare 30px line, and on a cleared
    market every sample is zero -- so the path sat flush on the bottom
    border with half its stroke clipped outside the viewBox, and the tile
    looked empty. Most of a board is cleared, so most tiles showed
    nothing at all.

    A stat panel does not have that failure mode, because the NUMBER
    carries the panel and the sparkline is context behind it. A market
    with no findings reads "0" against a flat baseline, which is a
    result rather than a blank.
    """
    peak = max((v for _, v in points), default=0.0)
    colour = colour or colour_for_severity(peak)
    ident = f"sc{abs(hash((value, label, round(peak, 2)))) % 100000}"
    body = [
        f'<svg xmlns="http://www.w3.org/2000/svg" class="statcard" '
        f'width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        f'preserveAspectRatio="none" aria-hidden="true">',
        f'<defs><linearGradient id="{ident}" x1="0" x2="0" y1="0" y2="1">'
        f'<stop offset="0" stop-color="{colour}" stop-opacity=".22"/>'
        f'<stop offset="1" stop-color="{colour}" stop-opacity="0"/></linearGradient></defs>',
    ]
    if points:
        # the trend occupies the lower two thirds, inset by the stroke so a
        # flat zero line is drawn ON the floor rather than through it
        top, floor_y = height * 0.34, height - 3.0
        line, area = _path(points, width, floor_y - top, 0.0, max(100.0, peak))
        shift = f'transform="translate(0,{top:.2f})"'
        body.append(f'<path d="{area}" fill="url(#{ident})" {shift}/>')
        body.append(f'<path d="{line}" fill="none" stroke="{colour}" stroke-width="2" '
                    f'vector-effect="non-scaling-stroke" stroke-linejoin="round" {shift}/>')
        body.append(f'<line x1="0" y1="{floor_y:.2f}" x2="{width}" y2="{floor_y:.2f}" '
                    f'stroke="{colour}" stroke-opacity=".28" stroke-width="1" '
                    f'vector-effect="non-scaling-stroke"/>')
    body.append(f'<text x="10" y="{height * 0.42:.0f}" font-family="Helvetica Neue,Helvetica,Arial" '
                f'font-size="{height * 0.42:.0f}" font-weight="700" fill="{colour}">{value}</text>')
    body.append(f'<text x="{10 + len(value) * height * 0.26:.0f}" y="{height * 0.42:.0f}" '
                f'font-family="ui-monospace,Menlo,monospace" font-size="{height * 0.145:.0f}" '
                f'letter-spacing="1.6" fill="{colour}" fill-opacity=".72">{label}</text>')
    body.append("</svg>")
    return "".join(body)


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
    return (f'<svg xmlns="http://www.w3.org/2000/svg" class="spark" '
        f'width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
            f'preserveAspectRatio="none" aria-hidden="true">{"".join(out)}</svg>')


# A note on why this module exists at all, corrected 2026-08-25 after
# checking the actual response headers rather than trusting a comment:
#
# Grafana Cloud blocks framing with `x-frame-options: deny`, NOT with a
# CSP `frame-ancestors` directive -- its CSP has no frame-ancestors at
# all. The difference is not pedantry. frame-ancestors can enumerate
# permitted origins, so "have Grafana allow-list our Cloud Run domain"
# would have been a real avenue worth pursuing. `deny` takes no origin
# list: it refuses every parent, always. There is no configuration that
# opens it.
#
# So drawing here is not a workaround for a setting nobody got round to
# changing. It is the only route.
