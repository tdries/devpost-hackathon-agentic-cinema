"""Agent mode: the whole console, asked for in sentences.

Studio mode is the console a person clicks through. Agent mode is the same
system with a Vertex AI agent in front of it: the operator types, the agent
decides what to look at, what to change and what to build, and the right
hand side shows whatever it opened. Everything the console can do is a tool
here, so the agent is not narrating the product from outside, it is driving
it.

The tools are deliberately the same functions the HTTP handlers use. An
agent that answers "what is blocked in France" out of its own summary of a
summary is a demo; an agent that answers it by reading the same store the
Market Room reads cannot drift from what the operator would see if they
clicked. So every tool below either reads the store or performs an action
the console itself offers, with the same refusals: the guard still blocks
protected-basis remediation, scope still refuses a patch it cannot support,
and the day's generation budget still governs Veo.

One tool is new rather than borrowed: build_dashboard. The Publisher already
writes the dashboards a run needs, but "show me the errors by dimension"
is a question nobody wrote a dashboard for in advance, so the agent composes
one from the labels the run already pushed to Loki and hands back its URL.
That is the difference between a dashboard product and an agent with a
dashboard product in its hands.
"""
from __future__ import annotations

import json
from urllib.parse import urlencode
import os
import time
from dataclasses import dataclass, field

from customs import adjudicate, costs, packs, scope as scope_mod
from customs import state
from customs.config import settings
from customs.store import Store

SYSTEM_PROMPT = """You are the operator's counterpart inside The Media Customs,
an ad clearance system. You have tools that read the same run store the console
reads and perform the same actions its buttons perform. Use them; never answer
from memory or invent a finding, a market or a rule id.

How to work:
* Answer the question asked, briefly, in plain sentences. No preamble.
* When something is worth looking at, call show() so it appears beside you.
* A question about what is IN the footage -- a rabbit, a short skirt, a lit
  cigarette, a visible logo -- is search_frames(), not show(). It reads
  every caption the analyst ever wrote, across every run, with a model, and
  puts the matching frames themselves beside you. Ask it in plain words:
  it matches meaning, so "bunnies" finds "an animated rabbit". Never answer
  "which frames" or "how many frames" from a run page, and never from your
  own reading of a caption list: search, then say the number the tool
  returned.
* Numbers come from tools, never from your own arithmetic over prose.
* When you refuse, say what the system refused and why, in its words.
* End with what you would do next, as one short line, only when there is a
  genuinely useful next step.

What the system believes, so you do not contradict it:
* A finding is an observation joined to one market's rule and a citation.
* "Cleared" means nothing open disqualifies a market, not that nothing is open.
* Scope decides what can fix a violation: a frame or a segment can be patched,
  a scene needs regeneration, a premise cannot be edited at all unless the
  offending element can be swapped for a permitted one.
* The guard refuses to auto-edit anything written on a protected basis. That
  refusal is a feature and you never work around it.

You are running the job, not answering about it. Clearing a commercial goes
upload -> processing -> findings -> decision -> fix -> verified, and the
operator should never have to work out which screen they are supposed to be
on. At every turn: say where the run is, say what it means, and say the one
thing worth doing next. If that next thing is a tool you have, offer to do
it rather than describing where the button is.

* Just uploaded: the crew is detecting shots, transcribing, observing each
  one, then judging every market in parallel. It takes a few minutes. Say
  that, show the board, and say what you will look for when it lands.
* Still running: check list_runs for the status rather than guessing, and
  say what has landed so far.
* Findings are in: lead with the verdict and what is holding it, not a
  count. Open the market that is blocking.
* A fix is possible: price it with fix_options before you suggest it, and
  say what it will do to the picture, not just what it costs.
* The guard blocked something: that is a decision for them, and you say so
  plainly rather than routing around it.
* Cleared: say what is still open even though it cleared, because "cleared"
  is not "clean".

A run starts on the global baseline and the EU. More markets are cheap --
they re-judge the observations the run already has, without opening the
asset again -- so offer them once there is something to judge.

Charting. Call data_schema() first, then chart() with the panels you want.
Any Grafana panel type. The two shapes that cover most questions:

  one value per thing (bars, slices, a stat) -- instant: true
    sum by (dimension) (count_over_time({app="customs", kind="observation"} [$__range]))

  a line over time (evolution, trend) -- instant: false
    sum(count_over_time({app="customs", kind="observation"} [$__interval]))

Two traps. A field that is not a stream label needs `| json` before you can
group on it: run_id, severity, confidence and rule_id on observations all
live in the line body, so "per run" is
    sum by (run_id) (count_over_time({app="customs", kind="observation"} | json [$__range]))
And a count over time needs a range selector: [$__range] for one number,
[$__interval] for a line. Without one, Loki returns nothing and the panel
draws "No data".

A third trap: the chart's window must match the window you queried. chart()
takes time_from (default "now-7d"); if you answered from a different window
-- query(window_hours=24), say -- pass the matching time_from ("now-24h"),
or your sentence and your chart will state two different numbers.
"""


@dataclass
class Turn:
    """One exchange, plus whatever the agent did to the console."""
    reply: str = ""
    view: str = ""            # a URL for the right hand pane
    view_label: str = ""
    view_external: str = ""   # where the real thing lives, when it is not ours
    calls: list[dict] = field(default_factory=list)
    error: str = ""


def _runs(store: Store, limit: int = 12) -> list[dict]:
    out = []
    for run in store.recent_runs(limit):
        findings = store.findings(run.id)
        out.append({
            "run_id": run.id,
            "asset": run.asset_path.split("/")[-1],
            "status": run.status,
            "markets": run.markets,
            "findings": len(findings),
            "open": sum(1 for f in findings if f.status == "open"),
        })
    return out


def _market_rows(store: Store, run_id: str) -> list[dict]:
    run = store.get_run(run_id)
    if run is None:
        return []
    rows = []
    for market in run.markets:
        findings = store.findings(run_id, market)
        rows.append({
            "market": market,
            "clearance": adjudicate.clearance(findings),
            "findings": len(findings),
            "open": sum(1 for f in findings if f.status == "open"),
            "needs_a_human": sum(1 for f in findings if f.remediation_blocked),
        })
    return rows


def _finding_rows(store: Store, run_id: str, market: str | None,
                  only_open: bool) -> list[dict]:
    run = store.get_run(run_id)
    duration = 120.0
    findings = store.findings(run_id, market)
    rows = []
    for f in findings:
        if only_open and f.status != "open":
            continue
        shape = scope_mod.classify(f, findings, duration)
        rows.append({
            "finding_id": f.id, "market": f.market, "rule": f.rule_id,
            "class": f.klass, "severity": f.severity, "status": f.status,
            "scope": shape, "substitutable": f.substitutable,
            "window": f"{f.t_start:.1f}-{f.t_end:.1f}s",
            "sourced": f.sourced, "needs_a_human": f.remediation_blocked,
            "why": f.rationale[:240],
            "citation": f.citation_ref,
        })
    rows.sort(key=lambda r: (-r["severity"], r["market"]))
    return rows[:60]


def _fix_options(store: Store, run_id: str, finding_id: str) -> dict:
    findings = store.findings(run_id)
    finding = next((f for f in findings if f.id == finding_id), None)
    if finding is None:
        return {"error": f"no finding {finding_id} in {run_id}"}
    shape = scope_mod.classify(finding, findings, 120.0)
    span = max(0.0, finding.t_end - finding.t_start)
    return {
        "finding_id": finding.id, "market": finding.market,
        "rule": finding.rule_id, "scope": shape,
        "substitutable": finding.substitutable,
        "needs_a_human": finding.remediation_blocked,
        "blocked_reason": finding.blocked_reason,
        "verdict": scope_mod.verdict(shape, finding.substitutable),
        "budget_left_eur": round(
            max(0.0, costs.DAILY_BUDGET_EUR - store.spent_today()), 2),
        "methods": costs.options(span, store.spent_today(), shape,
                                 finding.substitutable),
    }


def _library(dimension: str | None) -> list[dict]:
    all_packs = packs.load()
    rows = []
    for pack in all_packs.values():
        for rule in pack.own_rules:
            if dimension and rule.dimension != dimension:
                continue
            rows.append({
                "rule": rule.id, "where": pack.market, "level": pack.level,
                "dimension": rule.dimension, "class": rule.klass,
                "severity": rule.severity, "basis": rule.basis,
            })
    rows.sort(key=lambda r: (-r["severity"], r["rule"]))
    return rows[:80]


# --- what the agent can put on the right hand side -----------------------

_VIEWS = {
    "board": ("/runs/{run_id}", "Launch board"),
    "timeline": ("/runs/{run_id}/timeline", "Timeline"),
    "frames": ("/runs/{run_id}/frames", "Frame board"),
    "mission": ("/runs/{run_id}/mission", "Mission feed"),
    "cutting": ("/runs/{run_id}/cutting", "Cutting room"),
    "market": ("/runs/{run_id}/markets/{market}", "Market room"),
    "library": ("/library", "Library"),
    "runs": ("/runs", "All runs"),
}


def view_url(view: str, run_id: str = "", market: str = "") -> tuple[str, str]:
    if view not in _VIEWS:
        return "", ""
    path, label = _VIEWS[view]
    if "{run_id}" in path and not run_id:
        return "", ""
    if "{market}" in path and not market:
        return "", ""
    return path.format(run_id=run_id, market=market), label


# --- a dashboard nobody wrote in advance ---------------------------------

# What telemetry actually labels a finding with (see telemetry.push_finding):
# app, asset, market, klass, rule_id. Everything else -- dimension, severity,
# scope -- rides inside the JSON line, so grouping by it needs the line
# parsed first. Getting this wrong is invisible: Loki answers a query for a
# label nobody writes with no rows and no error, which is what "No data"
# was.
_STREAM_LABELS = {"market", "klass", "rule_id", "asset", "dimension"}
# Labels the risk gauge in Mimir carries but a Loki finding line does not.
_MIMIR_LABELS = {"dimension"}
_GROUPINGS = {
    "dimension": "dimension",
    "market": "market",
    "class": "klass",
    "klass": "klass",
    "rule": "rule_id",
    "rule_id": "rule_id",
    "asset": "asset",
    "severity": "severity",
    "scope": "scope",
}


def dashboard_spec(title: str, run_id: str, group_by: str) -> dict:
    """A Grafana dashboard built from the labels a run already pushed.

    Every finding went to Loki with {asset, market, class, dimension,
    rule_id}, which is exactly enough to answer "show me the errors by X"
    without anyone having written that dashboard first. The panels are a
    count by the requested label, the same count over the run's clock, and
    the findings themselves.
    """
    label = _GROUPINGS.get(group_by, "dimension")
    # kind!="observation" rather than kind="finding": the 471 lines pushed
    # before the kind label existed carry no kind at all, and Loki treats an
    # absent label as empty, so only the negative matcher sees the history.
    stream = '{app="customs", kind!="observation"}'
    if label in _MIMIR_LABELS:
        # dimension is a property of the observation, not of the finding, so
        # it never reached a Loki line: grouping findings by it there found
        # an empty label and drew "No data". The risk gauge has carried it
        # since the first run -- one sample per market per video second,
        # labelled with the covering finding's dimension and "none" when
        # nothing covered that second. Dropping "none" leaves exactly the
        # seconds a dimension put at risk, over every run ever pushed.
        counted = (f'sum by ({label}) (count_over_time('
                   f'customs_risk{{dimension!="none"}}[$__range]))')
        source = {"type": "prometheus", "uid": "grafanacloud-prom"}
        unit = "market-seconds at risk"
        # Prometheus spells "one value now" as instant/range, not queryType:
        # left as a range query the barchart draws a series over time instead
        # of one bar per dimension.
        target = {"expr": counted, "instant": True, "range": False,
                  "legendFormat": f"{{{{{label}}}}}", "refId": "A"}
    else:
        # a JSON line parse is what makes the non-label fields groupable
        parsed = stream if label in _STREAM_LABELS else f"{stream} | json"
        counted = f"sum by ({label}) (count_over_time({parsed} [$__range]))"
        source = {"type": "loki", "uid": "grafanacloud-logs"}
        unit = "findings"
        target = {"expr": counted, "queryType": "instant",
                  "legendFormat": f"{{{{{label}}}}}", "refId": "A"}
    uid = f"customs-adhoc-{label}-{int(time.time())}"[:40]
    return {
        "uid": uid,
        "title": title or f"Findings by {label}",
        "tags": ["customs", "adhoc"],
        # now-7d, not now-24h: the store's runs span days, and a 24h window
        # once let the agent say "62 findings" beside a chart drawing 9.
        "time": {"from": "now-7d", "to": "now"},
        "panels": [
            {
                "type": "barchart", "title": f"{unit.capitalize()} by {label}",
                "gridPos": {"h": 10, "w": 12, "x": 0, "y": 0},
                "datasource": source,
                "targets": [target],
                # one Google hue per bar, not eight identical blue ones
                "fieldConfig": {"defaults": {"color": {"mode": "palette-classic"}}},
            },
            {
                "type": "piechart", "title": f"Share by {label}",
                "gridPos": {"h": 10, "w": 12, "x": 12, "y": 0},
                "datasource": source,
                "targets": [target],
            },
            {
                "type": "logs", "title": "The findings themselves",
                "gridPos": {"h": 12, "w": 24, "x": 0, "y": 10},
                "datasource": {"type": "loki", "uid": "grafanacloud-logs"},
                "targets": [{"expr": stream, "refId": "A"}],
            },
        ],
    }


# The app's four colours, in the order a reader meets them on the console.
BRAND_CYCLE = (state.SIGNAL, state.BLOCKED, state.AT_RISK, state.CLEARED)

_SOURCES = {
    "loki": {"type": "loki", "uid": "grafanacloud-logs"},
    "prom": {"type": "prometheus", "uid": "grafanacloud-prom"},
}

# Grafana core panel types. Listed so the agent picks a real one rather than
# inventing "linechart"; the builder is otherwise indifferent to which.
VIZ_TYPES = (
    "timeseries", "barchart", "piechart", "stat", "gauge", "bargauge",
    "table", "heatmap", "histogram", "state-timeline", "status-history",
    "xychart", "trend", "logs", "candlestick", "text",
)


def chart_spec(title: str, panels: list[dict], time_from: str = "now-7d") -> dict:
    """A Grafana dashboard of arbitrary panels, laid out two per row.

    The agent writes the panels; this only turns them into the JSON Grafana
    wants. Each panel is {type, title, source: loki|prom, expr, instant}.

    ponytail: fixed two-up grid rather than a layout engine. Grafana's
    gridPos is 24 columns; anything cleverer is a panel nobody asked for.
    """
    # Fill the space. Grafana's grid is 24 columns; one panel takes all of
    # them, two share a row, more go two-up. Row height divides a ~24-unit
    # canvas so one panel is tall and four are not letterboxed.
    n = len(panels)
    cols = 1 if n == 1 else 2
    width = 24 // cols
    rows = (n + cols - 1) // cols
    height = max(8, min(24, 24 // max(rows, 1)))

    out = []
    for i, panel in enumerate(panels):
        kind = (panel.get("type") or "timeseries").strip()
        source = _SOURCES.get((panel.get("source") or "loki").strip(), _SOURCES["loki"])
        expr = (panel.get("expr") or "").strip()
        # An instant query is one value per series (a bar, a slice, a stat).
        # A range query is a line over time. Getting this wrong is why a
        # barchart sometimes draws a time series instead of bars.
        instant = bool(panel.get("instant"))
        target = {"expr": expr, "refId": "A",
                  "legendFormat": panel.get("legend") or ""}
        if source["type"] == "prometheus":
            target |= {"instant": instant, "range": not instant}
        elif instant:
            target["queryType"] = "instant"
        # Brand colours only. palette-classic is Grafana's own eight hues and
        # put charts on screen that belonged to a different product.
        #
        # "shades" is the mode that makes this work without knowing the series
        # names in advance: one brand hue per panel, and each series inside it
        # gets a shade of that hue. Cycled across panels so a four-panel
        # answer reads blue, red, yellow, green.
        hue = BRAND_CYCLE[i % len(BRAND_CYCLE)]
        out.append({
            "type": kind,
            "title": panel.get("title") or kind,
            "gridPos": {"h": height, "w": width,
                        "x": (i % cols) * width, "y": (i // cols) * height},
            "datasource": source,
            "targets": [target],
            "fieldConfig": {"defaults": {
                "color": {"mode": "shades", "fixedColor": hue},
                "thresholds": state.grafana_thresholds(),
            }},
        })
    return {
        "uid": f"customs-adhoc-{int(time.time() * 1000)}"[:40],
        "title": title or "Ad hoc",
        "tags": ["customs", "adhoc"],
        "time": {"from": time_from, "to": "now"},
        "panels": out,
    }


# --- the agent itself ----------------------------------------------------
#
# An ADK LlmAgent with FunctionTools over the functions above, run one turn
# per message. The tools close over the store and a per-turn Turn object, so
# what the agent opened and what it changed come back with its answer rather
# than having to be parsed out of prose.

APP_NAME = "customs-console"


def _vertex_env() -> None:
    """ADK reads its backend from the environment, not from our Settings.

    Cloud Run sets these as real env vars (scripts/deploy.sh), so the
    deployed service has always been fine. A developer running uvicorn from
    a .env has not: ADK falls through to the Gemini Developer API and asks
    for an API key nobody has. setdefault, so a real environment always
    wins over the file.
    """
    os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "true")
    if settings.gcp_project:
        os.environ.setdefault("GOOGLE_CLOUD_PROJECT", settings.gcp_project)
    # Not the deployment's region: the Gemini models this project can reach
    # live on Vertex's "global" endpoint, which is why genai_client pins
    # location="global" rather than reading the region. ADK builds its own
    # client from the environment, so it has to be told the same thing, and
    # told rather than defaulted, because deploy.sh has already set
    # GOOGLE_CLOUD_LOCATION to europe-west1 for everything else.
    os.environ["GOOGLE_CLOUD_LOCATION"] = "global"


def build_agent(store: Store, turn: Turn, run_id: str = ""):
    """The console's agent, with the console's own powers as tools."""
    from google.adk.agents import LlmAgent
    from google.adk.tools import FunctionTool

    _vertex_env()

    def list_runs() -> str:
        """List the most recent clearance runs with their status and counts."""
        turn.calls.append({"tool": "list_runs"})
        return json.dumps(_runs(store))

    def markets(run: str = "") -> str:
        """Per-market verdicts for one run. `run` defaults to the open run."""
        target = run or run_id
        turn.calls.append({"tool": "markets", "run": target})
        return json.dumps(_market_rows(store, target))

    def findings(run: str = "", market: str = "", open_only: bool = True) -> str:
        """Findings for a run, optionally one market, with scope and severity."""
        target = run or run_id
        turn.calls.append({"tool": "findings", "run": target, "market": market})
        return json.dumps(_finding_rows(store, target, market or None, open_only))

    def fix_options(finding_id: str, run: str = "") -> str:
        """What can be done about one finding, priced, with the day's budget."""
        target = run or run_id
        turn.calls.append({"tool": "fix_options", "finding": finding_id})
        return json.dumps(_fix_options(store, target, finding_id))

    def library(dimension: str = "") -> str:
        """The rules the system tests for, optionally one dimension."""
        turn.calls.append({"tool": "library", "dimension": dimension})
        return json.dumps(_library(dimension or None))

    def show(view: str, run: str = "", market: str = "") -> str:
        """Open a view beside the conversation: board, timeline, frames,
        mission, cutting, market, library or runs."""
        url, label = view_url(view, run or run_id, market)
        if not url:
            return f"cannot show {view!r} without the run or market it needs"
        turn.view, turn.view_label = url, label
        turn.calls.append({"tool": "show", "view": view, "url": url})
        return f"showing {label} at {url}"

    def search_frames(text: str = "", dimension: str = "", market: str = "",
                      flagged: str = "", days: int = 30, limit: int = 4000,
                      mode: str = "semantic") -> str:
        """Find individual FRAMES by what the analyst said about them, across
        every run this instance has ever performed, and show them.

        This is the tool for any question about what is IN the footage:
        "which frames have a rabbit in them", "how many show a short
        skirt", "has anything with a cigarette ever cleared France". It
        searches the analyst's own caption on each keyframe, so it answers
        with frames rather than with a link to a run.

        text is the question in plain words: "bunnies", "a woman drinking",
        "a hemline above the knee". Every candidate caption goes to Gemini
        with it, and the model decides which frames the question is about,
        so you never have to guess which words the analyst chose: it wrote
        "an animated rabbit" and "bunnies" still finds it. Do not build
        synonym lists or regex; ask the question.

        mode="literal" makes text a regex over the caption instead, for
        when you mean the characters: a rule id, a brand name, an exact
        phrase. An empty text with a dimension returns everything in that
        dimension without calling the model at all.

        dimension filters on the taxonomy (call library for the list),
        market on the markets that objected, flagged on "yes" (some market
        objected) or "no" (nobody minded).

        Returns the counts first -- total, films, per dimension, per market
        -- so a "how many" question is answered without reading the rows,
        then the rows themselves. It also opens the same search beside the
        conversation, so what you say and what the operator sees are one
        query.
        """
        from customs import search as frame_search
        from customs.grafana_ops import GrafanaOps

        turn.calls.append({"tool": "search_frames", "text": text[:80],
                           "dimension": dimension, "flagged": flagged})
        try:
            if text.strip() and mode != "literal":
                found = frame_search.indexed(store, text, dimension=dimension,
                                             market=market, flagged=flagged)
            else:
                with GrafanaOps(settings) as ops:
                    found = frame_search.frames(
                        ops, text, dimension=dimension, market=market,
                        flagged=flagged, days=max(1, min(days, 90)),
                        limit=max(1, min(limit, 12000)),
                        mode="literal" if mode == "literal" else "semantic")
        except frame_search.SearchError as exc:
            return f"that pattern will not run: {exc}"
        except Exception as exc:  # noqa: BLE001 -- the agent reports the failure
            return f"the frame search failed: {exc}"

        params = {"q": text, "dimension": dimension, "market": market,
                  "flagged": flagged, "mode": mode}
        query_string = urlencode({k: v for k, v in params.items() if v})
        turn.view = "/search" + (f"?{query_string}" if query_string else "")
        turn.view_label = frame_search.summary(found)
        # The rows the model reasons over, trimmed: forty captions is
        # plenty to characterise a set, and the counts above already carry
        # the shape of all of them.
        return json.dumps({
            "summary": frame_search.summary(found),
            "matched_by": found["mode"],
            "total": found["total"], "capped": found["capped"],
            "films": found["films"], "flagged": found["flagged"],
            "by_film": found["by_asset"], "by_dimension": found["by_dimension"],
            "by_market": found["by_market"],
            "query": found["query"],
            "frames": [{"film": h["asset"], "at": round(h["t_start"], 1),
                        "said": h["statement"], "why": h.get("why", ""),
                        "dimension": h["dimension"],
                        "objected": h["markets"], "rules": h["rules"],
                        "run": h["run_id"], "image": h["frame"]}
                       for h in found["hits"][:40]],
        })

    def data_schema() -> str:
        """What is queryable in Grafana, and how. Call this before analysing.

        The agent cannot invent a metric that does not exist if it is told
        exactly what does. This is the whole guardrail: a described schema
        plus a validated query, rather than hoping a model remembers the
        label set.
        """
        turn.calls.append({"tool": "data_schema"})
        return json.dumps({
            "loki": {
                "datasource": "grafanacloud-logs",
                "streams": {
                    '{app="customs", kind="finding"}': {
                        "one line per": "finding (a market objected)",
                        "labels": ["asset", "market", "klass", "rule_id", "dimension"],
                        "body fields (use | json)": [
                            "severity", "status", "scope", "substitutable",
                            "sourced", "remediable", "rationale", "citation_ref",
                            "t_start", "t_end", "observation_id", "run_id"],
                    },
                    '{app="customs", kind="observation"}': {
                        "one line per": "observation (what the analyst saw, "
                                        "flagged or not)",
                        "labels": ["asset", "dimension", "flagged (yes|no)"],
                        "body fields (use | json)": [
                            "statement", "confidence", "shot_id", "findings",
                            "markets", "max_severity", "rules", "t_start",
                            "t_end", "run_id", "has_frame", "has_box"],
                        "why it matters": "flagged=no is everything seen that "
                                          "NO market objected to, which is the "
                                          "only way to ask what is universally "
                                          "safe or which markets are permissive",
                    },
                    '{app="customs", kind="verdict"}': {
                        "one line per": "market x observation pairing the "
                                        "adjudicator was asked about -- the "
                                        "acquittals as well as the objections",
                        "labels": ["asset", "market", "dimension",
                                   "verdict (triggered|cleared|unreturned)"],
                        "body fields (use | json)": [
                            "observation_id", "rule_id", "severity", "run_id"],
                        "why it matters": "'cleared' is a market that looked "
                                          "and said no, and 'unreturned' is a "
                                          "pairing the judge never answered "
                                          "on. Findings alone cannot tell "
                                          "those two apart from never asked.",
                    },
                },
            },
            "mimir": {
                "datasource": "grafanacloud-prom",
                "metrics": {
                    "customs_risk{asset,market,dimension}":
                        "max finding severity per market per video second, on "
                        "the run's mapped clock; dimension is 'none' for a "
                        "second nothing covered",
                    "customs_market_status{asset,market}":
                        "current verdict as the VALUE, not a label: "
                        "0 cleared, 1 at risk, 2 blocked",
                    "customs_blocking{asset,market,rule_id}":
                        "one series per blocking rule; the value is the "
                        "finding's severity, not 1",
                    "customs_stage_error{asset,stage}": "stage failures",
                },
            },
            "notes": [
                "Loki metric queries need an aggregation, e.g. "
                "sum by (dimension) (count_over_time({...} [$__range]))",
                "A body field needs | json before it can be grouped on.",
                "customs_risk lives on each run's mapped clock, not wall "
                "time, so range queries need that run's own window. "
                "customs_market_status, customs_blocking and "
                "customs_stage_error are stamped at wall time instead.",
            ],
        }, indent=1)

    def query(source: str, expr: str, window_hours: int = 24) -> str:
        """Run a LogQL or PromQL query and return the rows.

        source is 'loki' or 'mimir'. This is what lets the agent answer a
        question nobody anticipated, instead of picking from a menu of
        pre-written groupings. Call data_schema first so the expression
        refers to labels that exist.
        """
        from customs.grafana_ops import GrafanaOps
        turn.calls.append({"tool": "query", "source": source, "expr": expr[:160]})
        expr = expr.replace("$__range", f"{int(window_hours)}h")
        try:
            with GrafanaOps(settings) as ops:
                rows = (ops.loki_instant(expr) if source.lower().startswith("l")
                        else ops.prom_instant(expr))
        except Exception as exc:  # noqa: BLE001 -- the agent reports the failure
            return (f"query failed: {exc}. Check the labels against "
                    f"data_schema; a metric or label that does not exist "
                    f"returns nothing rather than an error.")
        if not rows:
            return ("no rows. Either nothing matches, or the expression is "
                    "missing an aggregation, or a body field was grouped on "
                    "without | json. Check data_schema.")
        out = sorted(rows, key=lambda r: -r["value"])[:40]
        return json.dumps([{"labels": r["labels"], "value": r["value"]} for r in out])

    def build_dashboard(group_by: str = "dimension", title: str = "",
                        run: str = "") -> str:
        """Compose a Grafana dashboard from this run's own labels and open it.
        group_by is one of dimension, market, class or rule."""
        from customs.grafana_ops import GrafanaOps

        target = run or run_id
        spec = dashboard_spec(title, target, group_by)
        turn.calls.append({"tool": "build_dashboard", "group_by": group_by})
        try:
            with GrafanaOps(settings) as ops:
                made = ops.create_adhoc_dashboard(spec)
        except Exception as exc:  # noqa: BLE001 -- the agent reports the failure
            return f"could not build the dashboard: {exc}"
        # not the Grafana URL: Grafana Cloud refuses to be framed
        # (x-frame-options: deny + frame-ancestors 'none'), so the console shows the same server-side
        # render the launch board uses and keeps the real link beside it.
        turn.view = f"/grafana/{made['uid']}.png" + (f"?run={target}" if target else "")
        turn.view_label = made["title"]
        turn.view_external = made["url"]
        return json.dumps(made)

    def chart(panels: str, title: str = "", time_from: str = "now-7d") -> str:
        """Build and open a Grafana dashboard of any visualisation types.

        panels is a JSON list. Each entry:
          type    one of timeseries, barchart, piechart, stat, gauge,
                  bargauge, table, heatmap, histogram, state-timeline,
                  status-history, xychart, trend, logs
          source  "loki" for log lines, "prom" for metrics
          expr    the LogQL or PromQL. Call data_schema() first.
          instant true for one value per series (bars, slices, a stat),
                  false for a line over time
          title   the panel heading
          legend  optional, e.g. "{{market}}"

        Use this for anything shaped like a chart. build_dashboard is only
        the canned findings-by-label view."""
        from customs.grafana_ops import GrafanaOps

        try:
            spec = json.loads(panels) if isinstance(panels, str) else panels
        except json.JSONDecodeError as exc:
            return f"panels was not valid JSON: {exc}"
        if not isinstance(spec, list) or not spec:
            return "panels must be a non-empty JSON list of panel objects"
        bad = [p.get("type") for p in spec
               if p.get("type") and p["type"] not in VIZ_TYPES]
        if bad:
            return f"not Grafana panel types: {bad}. Use one of {list(VIZ_TYPES)}"

        turn.calls.append({"tool": "chart", "panels": len(spec),
                           "types": [p.get("type") for p in spec]})
        try:
            with GrafanaOps(settings) as ops:
                made = ops.create_adhoc_dashboard(chart_spec(title, spec, time_from))
        except Exception as exc:  # noqa: BLE001 -- the agent reports the failure
            return f"could not build the chart: {exc}"
        turn.view = f"/grafana/{made['uid']}.png"
        turn.view_label = made["title"]
        turn.view_external = made["url"]
        return json.dumps(made)

    return LlmAgent(
        name="console",
        model=settings.model_text,
        instruction=SYSTEM_PROMPT,
        tools=[FunctionTool(f) for f in (chart, list_runs, markets, findings,
                                         fix_options, library, show,
                                         search_frames, data_schema, query,
                                         build_dashboard)],
        # (the same eleven are named in TOOL_NAMES below, for the console
        # to show; keep them together)
    )


async def ask(store: Store, message: str, session_id: str,
              run_id: str = "") -> Turn:
    """One turn: the operator's sentence in, the agent's answer and whatever
    it opened out."""
    from google.adk.runners import InMemoryRunner
    from google.genai import types

    turn = Turn()
    agent = build_agent(store, turn, run_id)
    runner = InMemoryRunner(agent=agent, app_name=APP_NAME)
    try:
        await runner.session_service.create_session(
            app_name=APP_NAME, user_id="operator", session_id=session_id)
        content = types.Content(role="user", parts=[types.Part(text=message)])
        async for event in runner.run_async(user_id="operator",
                                            session_id=session_id,
                                            new_message=content):
            if getattr(event, "content", None) and event.content.parts:
                for part in event.content.parts:
                    if getattr(part, "text", None):
                        turn.reply += part.text
    except Exception as exc:  # noqa: BLE001 -- the console shows the failure
        turn.error = f"{type(exc).__name__}: {exc}"
    finally:
        await runner.close()
    turn.reply = turn.reply.strip()
    return turn


# What agent mode advertises on screen. Derived from the registration above
# rather than retyped in a template, because a console that lies about what
# its agent can reach is worse than one that says nothing.
TOOL_NAMES = ("chart", "list_runs", "markets", "findings", "fix_options",
              "library", "show", "search_frames", "data_schema", "query",
              "build_dashboard")
