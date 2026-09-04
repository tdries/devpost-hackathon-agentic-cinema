"""Everything this system keeps in Grafana, in one place a person can read.

The console's Grafana story is told in fragments: a panel on the board, a
chip on the market room, a sentence in the README. Nobody could see the
whole surface at once, which for a project whose claim is "Grafana is a
participant, not a picture" is the one inventory worth having.

Assembled from the definitions the crew provisions FROM rather than from a
list somebody typed:

  dashboards   parsed out of grafana/dashboards/*.json, the same files
               GrafanaOps.ensure_dashboards pushes
  alerting     imported from grafana_ops (ALERT_RULES, the group interval,
               the contact point, the route label)
  transports   imported from grafana_ops.MAPPING, which already records
               per operation which MCP tool it needs and what it does over
               REST when that tool is absent

Two things are declared here because their names live inside telemetry.py
as string literals at the push sites: the metric series and the Loki
streams. tests/test_grafana_map.py reads telemetry.py and fails if it ever
writes a series or a stream kind this file does not name, so the drift is
caught rather than trusted.

Nothing here touches the network. The page it feeds is a reading of what
this console provisions and reads back, not a live query of the stack: a
round trip per section would make the one page whose job is to be legible
the slowest page in the console.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from customs import grafana_ops
from customs.config import settings

DASHBOARD_DIR = grafana_ops.DASHBOARD_DIR


@dataclass(frozen=True)
class Panel:
    title: str
    kind: str
    store: str
    query: str


@dataclass(frozen=True)
class Dashboard:
    uid: str
    title: str
    description: str
    panels: tuple[Panel, ...]
    public_url: str
    shown_in: str


# Where each dashboard is met inside the console, so the inventory answers
# "and where do I see this one?" rather than only "does it exist?".
_SHOWN_IN = {
    "customs-overview": "Launch board, as a rendered panel and a live embed; "
                        "public link on every board",
    "customs-timeline": "Launch board and the run's Timeline tab; public link",
    "customs-lanes": "Launch board's lane strip, and every archive card's "
                     "squares (rendered to PNG when the viewer is not deployed)",
    "customs-grid": "Timeline tab, as the scene-by-dimension grid the console "
                    "draws its own axes around",
    "customs-history": "Archive, one live panel for the whole instance",
    "customs-findings": "Not embedded: the raw finding stream, for reading in "
                        "Grafana itself",
    "customs-market": "Not embedded: one market in full, for reading in "
                      "Grafana itself",
    "customs-remediation": "Not embedded: what was changed and what it closed, "
                           "with the annotations on it",
}

_PUBLIC = {
    "customs-overview": settings.grafana_public_overview,
    "customs-timeline": settings.grafana_public_timeline,
}


def _query_of(target: dict) -> str:
    """The one line that says what a panel asks for."""
    for key in ("expr", "query", "target"):
        value = target.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


@lru_cache(maxsize=1)
def dashboards() -> tuple[Dashboard, ...]:
    """Every dashboard this project provisions, with its panels.

    Read off disk once. These are the files ensure_dashboards pushes, so a
    panel renamed in Grafana by hand does not appear here -- which is the
    honest answer: the next provision would rename it back.
    """
    found = []
    for path in sorted(DASHBOARD_DIR.glob("*.json")):
        raw = json.loads(path.read_text())
        body = raw.get("dashboard", raw)
        panels = []
        for panel in body.get("panels", []):
            source = panel.get("datasource")
            store = source.get("type", "") if isinstance(source, dict) else str(source or "")
            targets = panel.get("targets") or [{}]
            panels.append(Panel(
                title=panel.get("title") or "(untitled)",
                kind=panel.get("type", ""),
                store={"loki": "Loki", "prometheus": "Mimir"}.get(store, "panel text"),
                query=_query_of(targets[0]),
            ))
        uid = body.get("uid", path.stem)
        found.append(Dashboard(
            uid=uid,
            title=body.get("title", uid),
            description=(body.get("description") or "").strip(),
            panels=tuple(panels),
            public_url=_PUBLIC.get(uid, ""),
            shown_in=_SHOWN_IN.get(uid, ""),
        ))
    return tuple(found)


# --- what is written, and onto which clock -----------------------------------
#
# The two clocks are telemetry.py's central rule and the reason this table
# has a `clock` column at all: alerting may only ever read current-clock
# series, and the timecode axis may only ever read the mapped-clock one.

SERIES = (
    {"name": "customs_risk", "clock": "mapped",
     "labels": ("asset", "market", "dimension"),
     "sample": "the worst severity in force at video second n, one sample per "
               "second of film, written at t0+n so the panel's x axis IS the "
               "timecode",
     "read_by": "Timeline dashboard, the lane strip, the grid"},
    {"name": "customs_market_status", "clock": "current",
     "labels": ("asset", "market"),
     "sample": "0 cleared, 1 at risk, 2 blocked: one market's answer right now",
     "read_by": "Clearance overview, and the alert that watches for a market "
                "going down"},
    {"name": "customs_blocking", "clock": "current",
     "labels": ("asset", "market", "rule_id"),
     "sample": "the severity of one open, sourced, blocking finding; it drops "
               "to zero when a remediation is verified, which is what resolves "
               "the alert",
     "read_by": "Overview, Market detail, Remediation, and the blocking alert"},
    {"name": "customs_stage_error", "clock": "current",
     "labels": ("asset", "stage"),
     "sample": "how many times one stage failed on this asset: a tool that "
               "silently skips a shot is worse than one that admits it",
     "read_by": "Overview, and the stage-error alert"},
)

STREAMS = (
    {"kind": "finding", "clock": "mapped",
     "labels": ("app", "asset", "market", "klass", "rule_id", "dimension", "kind"),
     "line": "the whole finding as JSON: rule, statute, citation, severity, "
             "the window it covers and whether the guard blocked it"},
    {"kind": "observation", "clock": "mapped",
     "labels": ("app", "asset", "dimension", "flagged", "kind"),
     "line": "what the analyst saw at that second, whether any market minded, "
             "and the rules that fired if one did"},
    {"kind": "verdict", "clock": "mapped",
     "labels": ("app", "asset", "market", "dimension", "verdict", "kind"),
     "line": "every market's answer about every observation, including the "
             "noes: a finding says France objected, a verdict says Germany "
             "looked at the same frame and did not",
     },
)

ANNOTATIONS = (
    {"what": "one per finding", "tags": "customs, asset, market, rule_id, id",
     "note": "drawn across the span the finding covers, on the run's mapped "
             "clock"},
    {"what": "one per remediation", "tags": "customs, asset, market, rule_id, "
                                            "id, resolved",
     "note": "written when a fix lands and the verifier confirms it"},
)

DATASTORES = (
    {"name": "Mimir", "logo": "mimir",
     "uid": grafana_ops.PROM_UID, "holds": "the four metric "
     "series above, pushed over OTLP", "written_with": "OTLP HTTP, not the API"},
    {"name": "Loki", "logo": "loki",
     "uid": grafana_ops.LOKI_UID, "holds": "the three line "
     "kinds above", "written_with": "the Loki push API"},
)

# How the console reads Grafana back. The write path is the interesting
# half of the claim, but a system that only writes is a system that could
# have written to a file: these are the calls that make Grafana the source
# rather than the destination.
READS = (
    {"call": "prom_range / prom_window / prom_instant",
     "does": "PromQL against Mimir through the datasource proxy",
     "used_for": "the board's own sparklines and the market room's lanes when "
                 "the app draws them itself"},
    {"call": "loki_lines / loki_instant",
     "does": "LogQL against Loki through the datasource proxy",
     "used_for": "the archive's history, and the agent's `query` tool"},
    {"call": "query_history",
     "does": "one rule's history for one brand, over thirty days",
     "used_for": "'has this brand tripped this rule before?', asked by the "
                 "agent and shown in the market room"},
    {"call": "render_png",
     "does": "server-side panel render at /render/d-solo/{uid}",
     "used_for": "every full-width Grafana panel in the console, because "
                 "Grafana Cloud refuses to be framed"},
    {"call": "embed_url",
     "does": "builds the windowed URL of a panel or dashboard",
     "used_for": "the links out, and the iframes when the viewer is deployed"},
)


def alerting() -> dict:
    """The alerting surface: two rules, one group, one contact point, one route."""
    return {
        "folder": f"{grafana_ops.FOLDER_TITLE} ({grafana_ops.FOLDER_UID})",
        "group": grafana_ops.ALERT_GROUP,
        "interval_s": grafana_ops.ALERT_GROUP_INTERVAL_S,
        "contact_point": grafana_ops.CONTACT_POINT_NAME,
        "route": "{}{}{}".format(*grafana_ops.ROUTE_LABEL),
        "rules": [
            {"uid": rule["uid"], "title": rule["title"], "expr": rule["expr"],
             "for": rule.get("for", "0s"),
             "group_by": ", ".join(rule.get("group_by", [])),
             "summary": rule["annotations"]["summary"]}
            for rule in grafana_ops.ALERT_RULES
        ],
    }


def operations() -> list[dict]:
    """Every write this system makes, and how it makes it.

    Straight out of grafana_ops.MAPPING, which is the table the runtime
    itself consults: an operation runs over MCP when the server advertises
    the tool it names, and over REST when it does not. An inventory that
    said "all of it is MCP" would be a flattering one.
    """
    rows = []
    for op, transport in grafana_ops.MAPPING.items():
        rows.append({
            "op": op,
            "mcp": transport.mcp_tool or "",
            "http": transport.http,
            "note": transport.note,
        })
    return rows


def stack() -> dict:
    """Which stack, and what of it is reachable without a token."""
    host = settings.grafana_url.rstrip("/")
    return {
        "url": host,
        "folder_uid": grafana_ops.FOLDER_UID,
        "folder_title": grafana_ops.FOLDER_TITLE,
        "viewer": settings.grafana_viewer_url,
        "public": [
            {"uid": uid, "url": url} for uid, url in _PUBLIC.items() if url
        ],
    }


def totals() -> dict:
    """The counts the page leads with."""
    boards = dashboards()
    return {
        "dashboards": len(boards),
        "panels": sum(len(d.panels) for d in boards),
        "series": len(SERIES),
        "streams": len(STREAMS),
        "rules": len(grafana_ops.ALERT_RULES),
        "operations": len(grafana_ops.MAPPING),
    }
