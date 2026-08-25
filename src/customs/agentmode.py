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
writes the six dashboards a run needs, but "show me the errors by dimension"
is a question nobody wrote a dashboard for in advance, so the agent composes
one from the labels the run already pushed to Loki and hands back its URL.
That is the difference between a dashboard product and an agent with a
dashboard product in its hands.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field

from customs import adjudicate, costs, packs, scope as scope_mod
from customs.config import settings
from customs.store import Store

SYSTEM_PROMPT = """You are the operator's counterpart inside The Media Customs,
an ad clearance system. You have tools that read the same run store the console
reads and perform the same actions its buttons perform. Use them; never answer
from memory or invent a finding, a market or a rule id.

How to work:
* Answer the question asked, briefly, in plain sentences. No preamble.
* When something is worth looking at, call show() so it appears beside you.
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
    stream = '{app="customs"}'
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
        "time": {"from": "now-24h", "to": "now"},
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
        # (frame-ancestors 'none'), so the console shows the same server-side
        # render the launch board uses and keeps the real link beside it.
        turn.view = f"/grafana/{made['uid']}.png" + (f"?run={target}" if target else "")
        turn.view_label = made["title"]
        turn.view_external = made["url"]
        return json.dumps(made)

    return LlmAgent(
        name="console",
        model=settings.model_text,
        instruction=SYSTEM_PROMPT,
        tools=[FunctionTool(f) for f in (list_runs, markets, findings,
                                         fix_options, library, show,
                                         build_dashboard)],
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
