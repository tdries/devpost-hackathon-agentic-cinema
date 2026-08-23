"""The Customs crew, as a Google ADK agent graph.

This is the ONLY module in the project that imports `google.adk`, so an ADK
API drift is a one-file fix (task-13-brief.md). Everything it runs already
exists and is already tested somewhere else: this module composes those
functions into agents, it does not reimplement any of them.

--- The graph (ADK 2.7.1) ---

    SequentialAgent "customs_crew"
      |- IngestAgent        "ingest"        BaseAgent
      |- AnalystAgent       "analyst"       BaseAgent
      |- ParallelAgent      "adjudicators"  one AdjudicatorAgent per market
      |     |- AdjudicatorAgent "adjudicator_FR"
      |     |- AdjudicatorAgent "adjudicator_SA"
      |- GuardAgent         "guard"         BaseAgent
      |- PublisherAgent     "publisher"     BaseAgent wrapping an LlmAgent

Which stage is an LlmAgent and which is a plain BaseAgent is not a stylistic
choice, it is where the model actually decides something:

* ingest, analyst, adjudicators and guard all have their model call (or the
  deliberate absence of one, in guard's case) *inside* the tested function
  they call. `analyst.observe_shot` is already a Gemini multimodal call with
  keyframes and audio bytes attached; wrapping it in an `LlmAgent` would mean
  re-plumbing image and audio parts through ADK's content pipeline to reach
  the same API with the same prompt. `adjudicate.judge` is already a batched
  Gemini call plus one grounded citation call per triggered rule. The value
  ADK adds at these stages is composition, fan-out and observability, not a
  second prompt loop, so they are custom `BaseAgent` subclasses whose
  `_run_async_impl` calls the tested function and yields ADK events.
* the publisher is the genuinely agentic one. It is an `LlmAgent` with five
  tools, three of which are live Grafana MCP calls. Its instruction suggests
  an order for those five steps, because the deterministic ones have a real
  dependency (the timeline push fixes the clock every later write maps onto)
  and because a wandering publisher is not an interesting demo. What the
  model actually owns is everything the order does not settle: it issues each
  call itself, chooses every argument, reads every result, decides what to do
  when one fails, judges whether all six dashboards came back, and composes
  the prose it writes into the overview dashboard's description. That last
  step is a real MCP write, on every run, in words no template produced.

--- ParallelAgent, and why SQLite survives it ---

`adjudicators` is a real `ParallelAgent`: each market's `judge()` runs in its
own thread (`asyncio.to_thread`), so N markets cost one market's latency, not
N. The sequential fallback the brief allows was not needed. Two things make
the shared `Store` safe under that fan-out:

1. `Store` opens its connection with `check_same_thread=False` and serializes
   every method on its own RLock. `sqlite3.threadsafety == 3` alone was not
   enough: it says the C library is serialized, not that two threads may
   interleave statements on one Python `Connection`, and doing that raises
   "bad parameter or other API misuse" (observed under two concurrent
   remediations in Task 14, which is when the lock was added).
2. The only writes the parallel branch makes are `store.emit` mission events,
   each a single INSERT plus COMMIT. Findings are collected in memory and
   persisted once, from the guard stage, on one thread.

--- What the ADK session state holds ---

Findings, observations and `Shot` objects are Python objects that stages hand
to each other through `_RunState`, a per-run scratchpad. The ADK session state
carries the small, JSON-safe summary of each stage (counts, per-market
clearance) via `EventActions.state_delta`, which is what makes the run legible
in `adk web` and in any ADK-native trace. Nothing reads state back out of the
session: the store remains the single source of truth, exactly as before.
"""
from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncGenerator

from google.adk.agents import BaseAgent, LlmAgent, ParallelAgent, SequentialAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.events.event import Event
from google.adk.events.event_actions import EventActions
from google.adk.models.google_llm import Gemini
from google.adk.runners import InMemoryRunner
from google.adk.tools import FunctionTool
from google.genai import types

from customs import pipeline, telemetry
from customs.config import settings
from customs.grafana_ops import GrafanaOps
from customs.media import probe_duration
from customs.schema import Finding, Observation, RunRecord
from customs.store import Store

APP_NAME = "customs"
USER_ID = "customs"

# The six dashboards grafana_ops provisions; the publisher is asked to confirm
# every one of them is on the stack before it writes to any of them.
DASHBOARD_UIDS = (
    "customs-overview", "customs-timeline", "customs-findings",
    "customs-market", "customs-remediation", "customs-history",
)
OVERVIEW_UID = "customs-overview"

# Every stage below reaches its work through the `pipeline` module rather than
# importing `observe_shot`/`judge`/`generate_json` directly. That is
# deliberate: pipeline.py is where those names have always been patchable, so
# every existing test that monkeypatches `pipeline.judge` still steers the
# crew, and pipeline.run's documented stage-error semantics keep exactly one
# definition instead of being re-derived here.

@dataclass
class _RunState:
    """The scratchpad one clearance run's stages hand along.

    Not the ADK session state: this holds live Python objects (Shot,
    Observation, Finding) that a JSON session state cannot. The session
    state gets the summary; this gets the payload.
    """
    markets: list[str]
    run_id: str = ""
    asset_path: str = ""
    duration: float = 0.0
    shots: list = field(default_factory=list)
    transcripts: dict[str, str] = field(default_factory=dict)
    observations: list[Observation] = field(default_factory=list)
    judged: dict[str, list[Finding]] = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)
    clearances: dict[str, str] = field(default_factory=dict)
    telemetry_summary: dict | None = None
    # Annotated Any, not threading.Lock: pydantic introspects this dataclass
    # when it becomes a field on the agents below, and threading.Lock is a
    # factory function rather than a type.
    lock: Any = field(default_factory=threading.Lock)

class _Stage(BaseAgent):
    """Shared plumbing for the four deterministic stages.

    Holds the run's Store and scratchpad, mirrors every mission event into
    the store exactly as pipeline.run always did, and keeps the ADK events
    minimal: one text event per stage plus the state_delta that summarises
    it.
    """
    store: Store
    state: _RunState
    workdir: Path

    def emit(self, agent: str, message: str) -> None:
        self.store.emit(self.state.run_id, agent, message)

    def _event(self, ctx: InvocationContext, text: str, state_delta: dict | None = None) -> Event:
        return Event(
            author=self.name,
            invocation_id=ctx.invocation_id,
            content=types.Content(role="model", parts=[types.Part(text=text)]),
            actions=EventActions(state_delta=state_delta or {}),
        )

class IngestAgent(_Stage):
    """Shot detection, micro-shot merge, and one transcription per shot.

    Shot detection runs once and is not stage-wrapped: an unreadable asset
    raises out of here, uncaught, which is pipeline.run's documented
    behaviour (there is no per-unit failure to skip when there are no shots
    at all). Each shot's transcription is its own retry-wrapped unit.
    """
    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        self.emit("pipeline", "ingest -> detecting shots")
        raw_shots, shots, duration = await asyncio.to_thread(self._detect)
        self.state.shots = shots
        self.state.duration = duration
        self.emit("pipeline", f"ingest -> {len(raw_shots)} raw shot(s) merged to {len(shots)}")

        await asyncio.to_thread(self._transcribe_all)
        await asyncio.to_thread(self._detect_flashes)
        yield self._event(
            ctx,
            f"ingest: {len(shots)} shot(s), {len(self.state.transcripts)} transcript(s), "
            f"{len(self.state.observations)} measured observation(s), {duration:.2f}s",
            {"shots": len(shots), "transcripts": len(self.state.transcripts),
             "measured_observations": len(self.state.observations),
             "duration": duration},
        )

    def _detect(self):
        raw_shots = pipeline.detect_shots(self.state.asset_path)
        shots = pipeline.merge_micro_shots(raw_shots)
        return raw_shots, shots, probe_duration(self.state.asset_path)

    def _detect_flashes(self) -> None:
        """The one observation this crew measures rather than asks a model for.

        Photosensitivity is a property of the frame sequence, so the analyst's
        keyframes cannot carry it (pipeline.flash_observations explains why).
        The result lands in state.observations here, in ingest, and the
        analyst stage appends its own model observations to it rather than
        replacing them. Its own retry-wrapped unit: a broken ffmpeg here
        records a stage error and costs the run its photosensitivity coverage,
        never the whole run.
        """
        ok, result = pipeline._call_with_retries(
            lambda: pipeline.flash_observations(self.state.asset_path, self.state.shots)
        )
        if not ok:
            self.emit("ingest", f"stage_error: flash detection: {result!r}")
            return
        self.state.observations.extend(result)
        for obs in result:
            self.emit("ingest", f"flash detector -> {obs.statement}")

    def _transcribe_all(self) -> None:
        for shot in self.state.shots:
            ok, result = pipeline._call_with_retries(
                lambda shot=shot: pipeline._transcribe_shot(
                    self.state.asset_path, shot, self.workdir
                )
            )
            if ok:
                self.state.transcripts[shot.shot_id] = result
                self.emit("transcription", f"{shot.shot_id} -> {len(result)} char(s)")
            else:
                self.emit("transcription", f"stage_error: {shot.shot_id}: {result!r}")

class AnalystAgent(_Stage):
    """One neutral observation pass per shot, then persist them.

    Calls `analyst.observe_shot` per shot rather than `analyst.observe_all`:
    observe_all would re-run shot detection that ingest has already done, and
    it calls observe_shot in a plain loop with no per-shot fault isolation,
    while design spec section 14 requires each model call to be retried three
    times and then skipped with a stage error. That per-shot retry is the
    whole reason pipeline.run never called observe_all either.
    """
    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        # extend, never replace: ingest's deterministic flash observations are
        # already in state.observations and are persisted here with the rest,
        # in one insert, so the store keeps exactly one copy of each.
        observations = list(self.state.observations)
        observations.extend(await asyncio.to_thread(self._observe_all))
        self.state.observations = observations
        self.store.add_observations(self.state.run_id, observations)
        self.emit("pipeline", f"analyst -> {len(observations)} observation(s) persisted")
        yield self._event(
            ctx, f"analyst: {len(observations)} observation(s)",
            {"observations": len(observations)},
        )

    def _observe_all(self) -> list[Observation]:
        observations: list[Observation] = []
        for shot in self.state.shots:
            ok, result = pipeline._call_with_retries(
                lambda shot=shot: pipeline.observe_shot(
                    self.state.asset_path, shot, self.workdir,
                    on_event=self.emit, transcripts=self.state.transcripts,
                )
            )
            if ok:
                observations.extend(result)
            else:
                self.emit("analyst", f"stage_error: {shot.shot_id}: {result!r}")
        return observations

class AdjudicatorAgent(_Stage):
    """One market's judging pass. One instance per market, run in parallel.

    The LLM judgment lives inside `adjudicate.judge` (a batched verdict call
    plus a grounded citation call per triggered rule). What ADK contributes
    here is the fan-out and the per-market fault isolation: a market whose
    judge() exhausts its retries records `stage_error: market={code}:` and the
    other markets keep going, which is the exact event shape
    pipeline.errored_markets reads.
    """
    market: str

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        text = await asyncio.to_thread(self._judge)
        yield self._event(ctx, text, {f"judged_{self.market}": text})

    def _judge(self) -> str:
        packs = pipeline.load_packs()
        pack = packs.get(self.market)
        if pack is None:
            self.emit("adjudicator", f"stage_error: market={self.market}: no market pack loaded")
            return f"{self.market}: no market pack loaded"

        ok, result = pipeline._call_with_retries(
            lambda: pipeline.judge(
                self.state.run_id, self.state.observations, pack, on_event=self.emit
            )
        )
        if not ok:
            self.emit("adjudicator", f"stage_error: market={self.market}: {result!r}")
            return f"{self.market}: stage error"

        with self.state.lock:
            self.state.judged[self.market] = result
        return f"{self.market}: {len(result)} raw finding(s)"

class GuardAgent(_Stage):
    """The rule layer, then persistence.

    No model call by design (design spec section 7): guard.apply is a pure
    function over findings and the market pack. Runs after the parallel
    branch has joined, so it is also the one place findings are written to
    SQLite, on a single thread.
    """
    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        packs = pipeline.load_packs()
        all_findings: list[Finding] = []
        for market in self.state.markets:
            pack = packs.get(market)
            judged = self.state.judged.get(market)
            if pack is None or judged is None:
                continue  # never judged: the adjudicator already emitted its stage_error
            findings = pipeline.apply_guard(judged, pack)
            all_findings.extend(findings)
            status = pipeline.clearance(findings)
            self.state.clearances[market] = status
            self.emit("adjudicator", f"{market} clearance -> {status} ({len(findings)} finding(s))")

        self.state.findings = all_findings
        self.store.add_findings(all_findings)
        self.emit(
            "pipeline",
            f"adjudicate -> {len(all_findings)} finding(s) persisted across "
            f"{len(self.state.markets)} market(s)",
        )
        blocked = sum(1 for f in all_findings if f.remediation_blocked)
        yield self._event(
            ctx,
            f"guard: {len(all_findings)} finding(s), {blocked} blocked from auto-remediation, "
            f"clearance {self.state.clearances}",
            {"findings": len(all_findings), "clearances": dict(self.state.clearances)},
        )

# --- the publisher's tools ---

class _OpsHolder:
    """One run's live GrafanaOps, shared between the publisher stage (which
    owns the connection's lifecycle) and the tool closures (which use it).

    A small object rather than a dict on purpose: the holder is also a field
    on a pydantic model, and pydantic validates a `dict` field by copying it,
    which would leave the agent setting `ops` on one holder while its tools
    read another. An arbitrary type is passed through by identity.
    """
    ops: GrafanaOps | None = None

def _ops_or_raise(holder: _OpsHolder) -> GrafanaOps:
    if holder.ops is None:
        raise RuntimeError("Grafana is not connected for this run")
    return holder.ops

def _mcp_or_raise(holder: _OpsHolder):
    ops = _ops_or_raise(holder)
    if ops.mcp is None or ops.mcp_error is not None:
        raise RuntimeError(f"mcp-grafana is not available: {ops.mcp_error}")
    return ops.mcp

def _mcp_result_text(answer: dict) -> str:
    """Flatten an MCP tool result to text for the model to read."""
    blocks = answer.get("content") or []
    text = "".join(b.get("text", "") for b in blocks if isinstance(b, dict))
    if answer.get("isError"):
        return f"ERROR: {text}"
    return text or "(empty result)"

def _publisher_tools(store: Store, state: _RunState, holder: _OpsHolder, emit) -> list[FunctionTool]:
    """The five tools the Publisher agent may call, in the order it is told
    about them.

    Two are deterministic FunctionTools over code that is already tested
    (telemetry.py, grafana_ops.py). Three are the raw Grafana MCP tools,
    reached through the same `_McpStdio` client grafana_ops already runs, so
    every one of them is real MCP traffic over the stdio server in
    `bin/mcp-grafana`, issued by the model with arguments it chose.

    Why FunctionTools rather than ADK's own `McpToolset`: see the module
    report. In short, `google.adk.tools.mcp_tool` needs the `mcp` package,
    which is not installed and is not in requirements.txt, so `McpToolset`
    cannot be imported at all in this environment; the brief's documented
    fallback is this path.
    """

    def failed(name: str, exc: Exception) -> str:
        """One tool call failing is reported to the model, not raised.

        Raising would propagate out of ADK's flow and end the publisher's
        whole turn, so a Grafana hiccup on step 4 would also throw away
        steps 1 to 3. Handing the model the error instead lets it say what
        broke and carry on, which is the point of making this stage an agent.
        The failure is still recorded as a stage error, so the Mission Log
        and customs_stage_error see it even though the run survives it.
        """
        emit("publisher", f"stage_error: publisher: {name} failed: {exc!r}")
        return f"ERROR: {name} failed: {exc!r}"

    async def push_run_telemetry() -> dict:
        """Push this clearance run's telemetry to Grafana Cloud.

        Writes, in one call: the mapped-clock customs_risk timeline for every
        market, the current-clock customs_market_status and customs_blocking
        series per market, one Loki log line per finding, one Grafana
        annotation per finding (deduplicated, and backed off if Grafana rate
        limits), and the customs_stage_error counter for any stage that
        failed. Call it once, before touching any dashboard.

        Returns:
            A summary of what was written.
        """
        try:
            return await asyncio.to_thread(_push_run_telemetry, store, state, emit)
        except Exception as exc:  # noqa: BLE001 -- reported to the model, see failed()
            return {"error": failed("push_run_telemetry", exc)}

    async def ensure_dashboards() -> dict:
        """Create or update the six Customs dashboards on the Grafana stack.

        Returns:
            A mapping of dashboard file name to the uid it now has.
        """
        try:
            result = await asyncio.to_thread(_ops_or_raise(holder).ensure_dashboards)
        except Exception as exc:  # noqa: BLE001
            return {"error": failed("ensure_dashboards", exc)}
        emit("publisher", f"ensure_dashboards -> {sorted(result.values())}")
        return result

    async def _call_mcp(name: str, arguments: dict, log: str) -> str:
        try:
            mcp = _mcp_or_raise(holder)
            answer = await asyncio.to_thread(mcp.call_tool, name, arguments)
        except Exception as exc:  # noqa: BLE001
            return failed(name, exc)
        emit("publisher", f"mcp {log}")
        return _mcp_result_text(answer)

    async def search_dashboards(query: str) -> str:
        """Search the Grafana stack for dashboards matching a query string.

        Args:
            query: The text to search dashboard titles for, e.g. "customs".

        Returns:
            The matching dashboards, with their titles, uids and folders.
        """
        return await _call_mcp("search_dashboards", {"query": query},
                               f"search_dashboards({query!r})")

    async def get_dashboard_by_uid(uid: str) -> str:
        """Read one dashboard's full JSON, including its description.

        Args:
            uid: The dashboard uid, e.g. "customs-overview".

        Returns:
            The dashboard JSON as text.
        """
        return await _call_mcp("get_dashboard_by_uid", {"uid": uid},
                               f"get_dashboard_by_uid({uid!r})")

    async def update_dashboard(uid: str, op: str, path: str, value: str) -> str:
        """Patch one property of an existing dashboard.

        Args:
            uid: The dashboard uid, e.g. "customs-overview".
            op: The patch operation: "replace", "add" or "remove".
            path: JSONPath of the property to change, e.g. "$.description".
            value: The new value for the property.

        Returns:
            Grafana's answer, including the new dashboard version.
        """
        return await _call_mcp(
            "update_dashboard",
            {
                "uid": uid,
                "operations": [{"op": op, "path": path, "value": value}],
                "message": f"customs run {state.run_id}",
            },
            f"update_dashboard({uid!r}, {op} {path})",
        )

    return [
        FunctionTool(push_run_telemetry),
        FunctionTool(ensure_dashboards),
        FunctionTool(search_dashboards),
        FunctionTool(get_dashboard_by_uid),
        FunctionTool(update_dashboard),
    ]

def _push_run_telemetry(store: Store, state: _RunState, emit) -> dict:
    """Every deterministic telemetry push for one run, in dependency order.

    push_timeline must go first and only once: it is what fixes this run's
    t0, and push_log/annotate map onto that same clock (telemetry.py's module
    docstring). The run record is re-read afterwards so the object handed to
    the later pushes carries the t0 that was just written.
    """
    if state.telemetry_summary is not None:
        # push_timeline re-picks this run's t0 on every call and overwrites
        # the stored one, stranding the customs_risk samples the first call
        # already wrote on an orphaned clock (telemetry.push_timeline's own
        # docstring). So a second call is answered, not performed.
        return dict(state.telemetry_summary, already_pushed=True)

    run = store.get_run(state.run_id)
    findings = state.findings
    telemetry.push_timeline(run, findings, state.duration, store)
    run = store.get_run(state.run_id)

    for market in state.markets:
        status = state.clearances.get(market)
        if status is None:
            continue  # market never evaluated; it has no clearance to report
        telemetry.push_status(run, market, status, findings)

    for finding in findings:
        telemetry.push_log(run, finding)

    # One annotation query for the whole run, not one per finding: the
    # per-finding loop is what got rate limited in Task 12.
    existing = telemetry.existing_annotation_keys(run)
    annotated = sum(1 for f in findings if telemetry.annotate(run, f, existing))

    # NB the summary below deliberately says "failed_stages", not
    # "stage_errors": it is emitted as a mission event, and both the CLI and
    # the tests pick stage errors out of the event log by searching for the
    # literal "stage_error".
    stages = _stage_error_stages(store, state.run_id)
    for stage in stages:
        telemetry.push_stage_error(run, stage)

    summary = {
        "run_id": state.run_id,
        "markets": list(state.markets),
        "clearances": dict(state.clearances),
        "risk_samples": f"{len(state.markets)} market(s) x {state.duration:.0f}s",
        "logs": len(findings),
        "annotations_created": annotated,
        "annotations_skipped_as_duplicates": len(findings) - annotated,
        "failed_stages": stages,
    }
    state.telemetry_summary = summary
    emit("publisher", f"push_run_telemetry -> {summary}")
    return summary

def _stage_error_stages(store: Store, run_id: str) -> list[str]:
    """The stage of every stage_error event this run recorded, in order.

    Read back off the run's own event log rather than tracked separately, so
    the customs_stage_error counter can never disagree with what the Mission
    Log shows.
    """
    return [
        agent
        for (_id, _ts, agent, message) in store.events_since(run_id, 0)
        if message.startswith("stage_error")
    ]

PUBLISHER_INSTRUCTION = """You are the Publisher agent of Customs, an ad clearance crew.

Clearance run {run_id} has finished. Asset: {asset}. Markets: {markets}.
Per-market clearance: {clearances}
Findings: {finding_count}. Stage errors: {stage_errors}.

Publish this run, in this order:
1. Call push_run_telemetry exactly once. It writes the risk timeline, the
   per-market status series, the Loki finding logs and the finding
   annotations.
2. Call ensure_dashboards exactly once, so the Customs dashboards exist.
3. Call search_dashboards with the query "customs" and check that all six of
   {dashboards} are present. Name any that are missing.
4. Call get_dashboard_by_uid for "{overview_uid}" and read its current
   description.
5. Call update_dashboard for "{overview_uid}" with op "replace" and path
   "$.description", writing a description that keeps the first sentence of
   the existing one and then states, in one sentence: latest run {run_id},
   asset {asset}, and the per-market clearance above.

Then stop and report in at most three lines: what you pushed, whether all six
dashboards were found, and the description you wrote. Never invent a
clearance status or a finding count: use only the numbers given above. If a
tool fails, say so plainly and carry on with the remaining steps."""

class PublisherAgent(BaseAgent):
    """The publisher stage: an LlmAgent, plus the two things around it that
    an LlmAgent cannot do itself.

    The stage node is a `BaseAgent` rather than the `LlmAgent` directly for
    two reasons, both required by the brief:

    1. *Lifecycle.* The Grafana MCP server is a subprocess. It is started
       when this stage starts and reaped when it ends, so a run never leaves
       an mcp-grafana behind, and `build()` stays free of side effects (no
       process spawn, no HTTP) so the wiring test can run offline.
    2. *Containment.* ADK propagates a sub-agent's exception up through the
       runner. A dead Grafana would therefore abort the whole invocation
       before the run reached status "done". Here it is caught, recorded as
       `stage_error: publisher`, and the run finishes with its findings
       persisted, which is the same contract every other stage has.
    """
    store: Store
    state: _RunState
    holder: _OpsHolder

    @property
    def llm(self) -> LlmAgent:
        """The LlmAgent this stage delegates to. It is a real sub-agent (not
        just a field) so ADK's own tree walks -- toolset collection and
        cleanup among them -- can see it."""
        return self.sub_agents[0]

    def emit(self, agent: str, message: str) -> None:
        self.store.emit(self.state.run_id, agent, message)

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        self.emit("publisher", "publisher -> connecting to Grafana")
        try:
            ops = await asyncio.to_thread(GrafanaOps, settings)
        except Exception as exc:  # noqa: BLE001 -- a dead Grafana must not fail the run
            self.emit("publisher", f"stage_error: publisher: {exc!r}")
            yield self._error_event(ctx, exc)
            return

        self.holder.ops = ops
        if ops.mcp_error:
            self.emit("publisher", f"publisher -> MCP unavailable, HTTP only: {ops.mcp_error}")
        try:
            async for event in self.llm.run_async(ctx):
                yield event
        except Exception as exc:  # noqa: BLE001 -- same containment for the agent's own turn
            self.emit("publisher", f"stage_error: publisher: {exc!r}")
            yield self._error_event(ctx, exc)
        finally:
            self.holder.ops = None
            await asyncio.to_thread(ops.close)

    def _error_event(self, ctx: InvocationContext, exc: Exception) -> Event:
        return Event(
            author=self.name,
            invocation_id=ctx.invocation_id,
            content=types.Content(
                role="model",
                parts=[types.Part(text=f"publisher stage error: {exc!r}")],
            ),
            actions=EventActions(state_delta={"publisher_error": repr(exc)}),
        )

class SkipPublisherAgent(BaseAgent):
    """The publisher node when publishing is switched off.

    Keeps the graph the same shape in every mode -- the five sub-agent names
    are the five sub-agent names whether or not this run talks to Grafana --
    so an offline test still exercises the real crew rather than a shorter
    one.
    """
    store: Store
    state: _RunState

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        self.store.emit(self.state.run_id, "publisher", "publisher -> disabled (publish=False)")
        yield Event(
            author=self.name,
            invocation_id=ctx.invocation_id,
            content=types.Content(role="model", parts=[types.Part(text="publisher: disabled")]),
        )

def _publisher_instruction(state: _RunState, store: Store):
    """An InstructionProvider rather than a templated string: the numbers it
    quotes are only known once the guard stage has run, which is after
    build() has already constructed this agent."""
    def provide(_ctx: ReadonlyContext) -> str:
        return PUBLISHER_INSTRUCTION.format(
            run_id=state.run_id,
            # the stem, not the file name: it is the `asset` label every
            # customs_* series and every finding annotation carries, so the
            # description names the asset the dashboards can be filtered by.
            asset=Path(state.asset_path).stem or state.asset_path,
            markets=", ".join(state.markets),
            clearances=", ".join(
                f"{m}={state.clearances.get(m, 'not evaluated')}" for m in state.markets
            ),
            finding_count=len(state.findings),
            stage_errors=len(_stage_error_stages(store, state.run_id)),
            dashboards=", ".join(DASHBOARD_UIDS),
            overview_uid=OVERVIEW_UID,
        )
    return provide

def _default_model() -> Gemini:
    """The same Vertex AI configuration genai_client.client() uses, so the
    publisher and the rest of the crew talk to one project and one endpoint."""
    return Gemini(
        model=settings.model_text,
        client_kwargs={
            "vertexai": True,
            "project": settings.gcp_project,
            "location": "global",
        },
    )

def _build_publisher(store: Store, state: _RunState, model) -> PublisherAgent:
    holder = _OpsHolder()

    def emit(agent: str, message: str) -> None:
        store.emit(state.run_id, agent, message)

    llm = LlmAgent(
        name="publisher_llm",
        model=model if model is not None else _default_model(),
        description="Publishes a clearance run to Grafana over MCP.",
        instruction=_publisher_instruction(state, store),
        tools=_publisher_tools(store, state, holder, emit),
        # The stage is a single publishing turn at the end of a fixed
        # pipeline, so there is no peer or parent worth transferring to, and
        # the prior stages' events are summarised in the instruction already.
        include_contents="none",
        disallow_transfer_to_parent=True,
        disallow_transfer_to_peers=True,
    )
    return PublisherAgent(name="publisher", store=store, state=state,
                          holder=holder, sub_agents=[llm])

def build(store: Store, workdir, markets: list[str], state: _RunState | None = None,
          *, publish: bool = True, model=None) -> SequentialAgent:
    """Compose the crew for one clearance run.

    Args:
        store: the run store every stage writes its mission events to.
        workdir: scratch directory for extracted frames and audio.
        markets: market codes; one adjudicator sub-agent is built per code.
        state: the per-run scratchpad. Defaults to a fresh empty one, which
            is enough to inspect the graph's shape but not to run it;
            run_clearance always passes the real one.
        publish: False replaces the Publisher with a node that records that
            it was switched off. The graph keeps the same five sub-agents.
        model: the LlmAgent's model. Defaults to settings.model_text on
            Vertex AI; tests inject a scripted BaseLlm.

    Returns:
        The root SequentialAgent, whose sub-agents are exactly
        ["ingest", "analyst", "adjudicators", "guard", "publisher"].

    Building the graph has no side effects: no model call, no HTTP, and no
    mcp-grafana subprocess. The Grafana connection is opened by the publisher
    stage itself, when it runs.
    """
    state = state if state is not None else _RunState(markets=list(markets))
    workdir = Path(workdir)
    common = {"store": store, "state": state, "workdir": workdir}

    publisher = (
        _build_publisher(store, state, model) if publish
        else SkipPublisherAgent(name="publisher", store=store, state=state)
    )
    return SequentialAgent(
        name="customs_crew",
        description="Clears one ad for a list of markets.",
        sub_agents=[
            IngestAgent(name="ingest", **common),
            AnalystAgent(name="analyst", **common),
            ParallelAgent(
                name="adjudicators",
                description="Judges every market at once, one agent per market.",
                sub_agents=[
                    AdjudicatorAgent(name=f"adjudicator_{m}", market=m, **common)
                    for m in markets
                ],
            ),
            GuardAgent(name="guard", **common),
            publisher,
        ],
    )

def run_clearance(asset_path, markets: list[str], store: Store, workdir,
                  *, publish: bool = True, model=None,
                  run_id: str | None = None) -> RunRecord:
    """Run one asset through the crew, via an ADK Runner, and return its run record.

    The run record, its events, its observations and its findings all land in
    the same Store, in the same shape, as they did when pipeline.run drove
    the stages itself; pipeline.run now delegates here.

    `run_id` adopts a run record that already exists instead of creating one.
    That is what Launch Control needs and nothing else uses: the console
    creates the run inside the upload request so it can redirect the browser
    straight to /runs/{id}, then hands the id to this function on a
    background thread. Without it the id would only exist once the crew had
    started, and the upload would have nowhere to send the browser. Passing
    an unknown id raises rather than quietly minting a second run.

    Stage errors never raise out of this function -- they are recorded as
    events and the run still reaches status "done" -- with the one documented
    exception pipeline.run has always had: shot detection failing on an
    unreadable asset raises, leaving the run in status "running", because
    there is no per-unit failure to skip when there are no shots at all.
    """
    workdir = Path(workdir)
    asset_path = str(asset_path)
    if run_id is None:
        run_record = store.create_run(asset_path=asset_path, markets=list(markets))
        run_id = run_record.id
    elif store.get_run(run_id) is None:
        raise ValueError(f"unknown run: {run_id}")

    store.set_run_t0(run_id, time.time())
    store.set_run_status(run_id, "running")
    store.emit(run_id, "pipeline",
               f"run {run_id} started: asset={asset_path} markets={list(markets)}")

    state = _RunState(markets=list(markets), run_id=run_id, asset_path=asset_path)
    root = build(store, workdir, list(markets), state, publish=publish, model=model)
    asyncio.run(_drive(root, run_id))

    store.set_run_status(run_id, "done")
    store.emit(run_id, "pipeline", f"run {run_id} done")
    return store.get_run(run_id)

async def _drive(root: SequentialAgent, run_id: str) -> None:
    """Drive the graph to completion through an ADK Runner.

    Runner.run()'s synchronous form runs the loop on a bare thread whose
    exceptions never reach the caller, which would turn a real ingest failure
    into a silently truncated run, so the async form is driven directly here.
    The events are drained rather than collected: every stage has already
    mirrored its own into the Store, which is what the console and the CLI
    read.
    """
    runner = InMemoryRunner(agent=root, app_name=APP_NAME)
    try:
        await runner.session_service.create_session(
            app_name=APP_NAME, user_id=USER_ID, session_id=run_id
        )
        message = types.Content(
            role="user", parts=[types.Part(text=f"Clear run {run_id}.")]
        )
        async for _event in runner.run_async(
            user_id=USER_ID, session_id=run_id, new_message=message
        ):
            pass
    finally:
        await runner.close()
