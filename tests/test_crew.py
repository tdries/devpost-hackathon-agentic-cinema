"""Tests for the ADK agent crew (src/customs/crew.py).

Offline tests never call a model and never spawn mcp-grafana: the two
external seams (`crew.GrafanaOps` and the publisher's LLM) are injected.
The graph itself is always the real one -- every offline run below goes
through a real ADK `Runner`, a real `SequentialAgent`, a real
`ParallelAgent` and the real custom `BaseAgent` subclasses.
"""
import subprocess
import threading
import time
from pathlib import Path
from typing import AsyncGenerator

import pytest
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.genai import types
from pydantic import Field

from customs import crew, pipeline, telemetry
from customs.schema import Finding, Observation
from customs.store import Store

# --- fixtures ---

@pytest.fixture(scope="session")
def clip(tmp_path_factory):
    # same two-shot silent clip recipe as tests/test_pipeline.py: offline,
    # one real cut, an audio track for extract_audio_span to slice.
    p = tmp_path_factory.mktemp("crew") / "clip.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi", "-i", "color=red:s=320x240:d=2",
        "-f", "lavfi", "-i", "color=blue:s=320x240:d=2",
        "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono",
        "-filter_complex", "[0:v][1:v]concat=n=2:v=1[v]",
        "-map", "[v]", "-map", "2:a", "-t", "4", "-pix_fmt", "yuv420p", str(p)],
        check=True, capture_output=True, timeout=60)
    return p

@pytest.fixture
def offline(request):
    """True for every test except the one marked live.

    Both guards below are keyed off this. A live test must reach the real
    Grafana and must really back off between retries; an offline test must do
    neither, and must fail loudly rather than quietly if it tries.
    """
    return request.node.get_closest_marker("live") is None

@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch, offline):
    if offline:
        monkeypatch.setattr(pipeline.time, "sleep", lambda seconds: None)

@pytest.fixture(autouse=True)
def no_grafana(monkeypatch, offline):
    """No offline test may construct a real GrafanaOps: that spawns the
    mcp-grafana subprocess and talks to Grafana Cloud. Tests that want a
    working publisher override this with their own fake."""
    if not offline:
        return
    def boom(*args, **kwargs):
        raise AssertionError("offline test constructed a real GrafanaOps")
    monkeypatch.setattr(crew, "GrafanaOps", boom)

# --- helpers ---

def _canned_observation(shot_id: str) -> Observation:
    return Observation(
        id=f"obs_{shot_id}", shot_id=shot_id, t_start=0.0, t_end=2.0,
        dimension="alcohol_tobacco_drugs", statement="A wine glass is visible.",
        evidence_frame="f.jpg", confidence=0.9,
    )

def _canned_finding(market: str, rule_id: str = "ZZ-01", run_id: str = "run_1") -> Finding:
    return Finding(
        id=f"fnd_{market}_{rule_id}", run_id=run_id, observation_id=f"obs_shot_0",
        market=market, rule_id=rule_id, klass="legal", severity=90,
        t_start=0.0, t_end=2.0, rationale="triggers the rule",
        citation_ref="basis", citation_url="https://example.com/x",
        sourced=True, remediable=True, remediation_blocked=False,
        blocked_reason="", status="open",
    )

def _fake_observe_shot(video_path, shot, workdir, on_event=None, transcripts=None):
    if on_event:
        on_event("analyst", f"observe -> {shot.shot_id}")
    return [_canned_observation(shot.shot_id)]

def _fake_transcribe(model, parts, schema):
    return {"transcript": "Twice the energy of any other drink."}

def _stub_stages(monkeypatch, judge=None):
    """Point the three model-calling stages at canned answers. Patched on
    the pipeline module because that is where crew.py looks them up, which
    is what keeps tests/test_pipeline.py's existing monkeypatches working
    against the ADK crew unchanged."""
    monkeypatch.setattr(pipeline, "generate_json", _fake_transcribe)
    monkeypatch.setattr(pipeline, "observe_shot", _fake_observe_shot)
    monkeypatch.setattr(
        pipeline, "judge",
        judge or (lambda run_id, observations, pack, on_event=None:
                  [_canned_finding(pack.market, run_id=run_id)]),
    )

def _messages(store, run_id):
    return [msg for (_id, _ts, _agent, msg) in store.events_since(run_id, 0)]

# --- Step 1: the wiring test ---

def test_build_sub_agent_names_are_exactly_the_five_stages_in_order(tmp_path):
    store = Store(tmp_path / "t.db")
    root = crew.build(store, tmp_path / "work", ["FR", "SA"])
    assert [a.name for a in root.sub_agents] == [
        "ingest", "analyst", "adjudicators", "guard", "publisher"
    ]

def test_build_fans_the_adjudicators_out_one_agent_per_market(tmp_path):
    store = Store(tmp_path / "t.db")
    root = crew.build(store, tmp_path / "work", ["FR", "SA", "US"])
    adjudicators = root.find_sub_agent("adjudicators")
    assert [a.name for a in adjudicators.sub_agents] == [
        "adjudicator_FR", "adjudicator_SA", "adjudicator_US"
    ]

def test_build_publisher_wraps_an_llm_agent_carrying_all_five_tools(tmp_path):
    store = Store(tmp_path / "t.db")
    root = crew.build(store, tmp_path / "work", ["FR"])
    publisher = root.find_sub_agent("publisher")
    assert [t.name for t in publisher.llm.tools] == [
        "push_run_telemetry", "ensure_dashboards",
        "search_dashboards", "get_dashboard_by_uid", "update_dashboard",
    ]

def test_build_makes_no_model_call_and_spawns_no_mcp_server(tmp_path):
    # the no_grafana fixture already fails the test if GrafanaOps is
    # constructed; this pins the other half: the model is only resolved,
    # never called, at build time.
    store = Store(tmp_path / "t.db")
    root = crew.build(store, tmp_path / "work", ["FR"])
    assert root.find_sub_agent("publisher").llm.model.model == crew.settings.model_text

# --- Step 2: running the graph ---

def test_run_clearance_persists_observations_and_findings_and_ends_done(monkeypatch, clip, tmp_path):
    _stub_stages(monkeypatch)
    store = Store(tmp_path / "t.db")
    run = crew.run_clearance(str(clip), ["FR", "SA"], store, tmp_path / "work", publish=False)

    assert run.status == "done"
    assert run.markets == ["FR", "SA"]
    assert {o.shot_id for o in store.observations(run.id)} == {"shot_0", "shot_1"}
    assert {f.market for f in store.findings(run.id)} == {"FR", "SA"}
    assert all(f.run_id == run.id for f in store.findings(run.id))

def test_run_clearance_emits_a_mission_event_for_every_stage(monkeypatch, clip, tmp_path):
    _stub_stages(monkeypatch)
    store = Store(tmp_path / "t.db")
    run = crew.run_clearance(str(clip), ["FR"], store, tmp_path / "work", publish=False)

    joined = "\n".join(_messages(store, run.id))
    assert "ingest" in joined
    assert "shot_0" in joined and "shot_1" in joined
    assert "observation(s) persisted" in joined
    assert "FR clearance ->" in joined
    assert "done" in joined

def test_run_clearance_runs_the_adjudicators_in_parallel(monkeypatch, clip, tmp_path):
    # Two markets must be in judge() at the same time. The barrier only
    # releases when both arrive, so a sequential fan-out times out here
    # rather than passing slowly.
    barrier = threading.Barrier(2, timeout=20)

    def judge_at_the_barrier(run_id, observations, pack, on_event=None):
        barrier.wait()
        return [_canned_finding(pack.market, run_id=run_id)]

    _stub_stages(monkeypatch, judge=judge_at_the_barrier)
    store = Store(tmp_path / "t.db")
    run = crew.run_clearance(str(clip), ["FR", "SA"], store, tmp_path / "work", publish=False)

    assert {f.market for f in store.findings(run.id)} == {"FR", "SA"}
    assert pipeline.errored_markets(store, run.id) == set()

def test_run_clearance_keeps_the_guard_wired(monkeypatch, clip, tmp_path):
    # SA-LGBT-01 is a real markets/SA.yaml rule with protected_basis: true.
    _stub_stages(monkeypatch, judge=lambda run_id, observations, pack, on_event=None: [
        _canned_finding(pack.market, rule_id="SA-LGBT-01", run_id=run_id)
    ])
    store = Store(tmp_path / "t.db")
    run = crew.run_clearance(str(clip), ["SA"], store, tmp_path / "work", publish=False)

    finding = store.findings(run.id, market="SA")[0]
    assert finding.remediation_blocked is True
    assert finding.status == "open"

def test_run_clearance_market_stage_error_never_takes_down_the_run(monkeypatch, clip, tmp_path):
    def flaky_judge(run_id, observations, pack, on_event=None):
        if pack.market == "SA":
            raise RuntimeError("simulated 5xx")
        return [_canned_finding(pack.market, run_id=run_id)]

    _stub_stages(monkeypatch, judge=flaky_judge)
    store = Store(tmp_path / "t.db")
    run = crew.run_clearance(str(clip), ["FR", "SA"], store, tmp_path / "work", publish=False)

    assert run.status == "done"
    assert pipeline.errored_markets(store, run.id) == {"SA"}
    assert [f.market for f in store.findings(run.id)] == ["FR"]

def test_two_consecutive_runs_into_one_store(monkeypatch, clip, tmp_path):
    # The failure that killed the first live attempt of this task: observation
    # ids come from a per-video shot index, so the second run of anything into
    # one database used to die with
    # "UNIQUE constraint failed: observations.id" before the store's
    # (run_id, id) primary key.
    _stub_stages(monkeypatch)
    store = Store(tmp_path / "t.db")

    first = crew.run_clearance(str(clip), ["FR"], store, tmp_path / "work", publish=False)
    second = crew.run_clearance(str(clip), ["FR"], store, tmp_path / "work", publish=False)

    assert first.id != second.id
    assert first.status == second.status == "done"
    for run in (first, second):
        assert {o.id for o in store.observations(run.id)} == {"obs_shot_0", "obs_shot_1"}
        assert [f.market for f in store.findings(run.id)] == ["FR"]
        assert all(f.run_id == run.id for f in store.findings(run.id))

# --- Step 2: the publisher must never take the run down with it ---

def test_publisher_failure_emits_a_stage_error_and_the_run_still_completes(monkeypatch, clip, tmp_path):
    def grafana_is_down(*args, **kwargs):
        raise RuntimeError("connection refused")
    monkeypatch.setattr(crew, "GrafanaOps", grafana_is_down)
    _stub_stages(monkeypatch)

    store = Store(tmp_path / "t.db")
    run = crew.run_clearance(str(clip), ["FR"], store, tmp_path / "work")

    assert run.status == "done", "a dead Grafana must never fail the clearance run"
    assert [f.market for f in store.findings(run.id)] == ["FR"], "findings must still be persisted"
    stage_errors = [m for m in _messages(store, run.id) if m.startswith("stage_error: publisher")]
    assert len(stage_errors) == 1, f"expected one publisher stage_error, got: {stage_errors}"
    assert "connection refused" in stage_errors[0]
    assert pipeline.errored_markets(store, run.id) == set(), (
        "a publisher failure is not a market failure: FR was evaluated"
    )

def test_publish_false_skips_the_publisher_but_keeps_it_in_the_graph(monkeypatch, clip, tmp_path):
    _stub_stages(monkeypatch)
    store = Store(tmp_path / "t.db")
    run = crew.run_clearance(str(clip), ["FR"], store, tmp_path / "work", publish=False)

    assert not [m for m in _messages(store, run.id) if m.startswith("stage_error")]
    assert any("publisher -> disabled" in m for m in _messages(store, run.id))

# --- Step 2: the publisher's agentic turn, with a scripted model ---

class _ScriptedLlm(BaseLlm):
    """A BaseLlm that replays a fixed list of responses, so the publisher's
    tool loop can be exercised with no network and no model spend."""
    model: str = "scripted"
    script: list = Field(default_factory=list)
    prompts: list = Field(default_factory=list)

    async def generate_content_async(self, llm_request, stream=False) -> AsyncGenerator[LlmResponse, None]:
        self.prompts.append(llm_request)
        step = self.script.pop(0) if self.script else "done"
        if isinstance(step, tuple):
            name, args = step
            part = types.Part(function_call=types.FunctionCall(name=name, args=args))
        else:
            part = types.Part(text=step)
        yield LlmResponse(content=types.Content(role="model", parts=[part]))

class _FakeOps:
    """Stands in for GrafanaOps: no subprocess, no HTTP."""
    def __init__(self, *args, **kwargs):
        self.mcp = self
        self.mcp_tools = {"search_dashboards", "get_dashboard_by_uid", "update_dashboard"}
        self.mcp_error = None
        self.calls = []
        self.closed = False

    def ensure_dashboards(self):
        self.calls.append(("ensure_dashboards", {}))
        return {"overview": "customs-overview"}

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return {"content": [{"type": "text", "text": f"{name} ok"}]}

    def close(self):
        self.closed = True

def test_publisher_llm_turn_drives_the_function_tools_and_the_mcp_tools(monkeypatch, clip, tmp_path):
    ops = _FakeOps()
    monkeypatch.setattr(crew, "GrafanaOps", lambda *a, **k: ops)
    pushed = []
    monkeypatch.setattr(crew.telemetry, "push_timeline", lambda *a, **k: pushed.append("timeline"))
    monkeypatch.setattr(crew.telemetry, "push_status", lambda *a, **k: pushed.append("status"))
    monkeypatch.setattr(crew.telemetry, "push_log", lambda *a, **k: pushed.append("log"))
    monkeypatch.setattr(crew.telemetry, "annotate", lambda *a, **k: bool(pushed.append("annotate")) or True)
    monkeypatch.setattr(crew.telemetry, "existing_annotation_keys", lambda run: set())
    _stub_stages(monkeypatch)

    llm = _ScriptedLlm(script=[
        ("push_run_telemetry", {}),
        ("ensure_dashboards", {}),
        ("search_dashboards", {"query": "customs"}),
        ("get_dashboard_by_uid", {"uid": "customs-overview"}),
        ("update_dashboard", {
            "uid": "customs-overview", "op": "replace", "path": "$.description",
            "value": "latest run",
        }),
        "published",
    ])

    store = Store(tmp_path / "t.db")
    run = crew.run_clearance(str(clip), ["FR"], store, tmp_path / "work", model=llm)

    assert run.status == "done"
    assert pushed == ["timeline", "status", "log", "annotate"]
    assert [name for name, _args in ops.calls] == [
        "ensure_dashboards", "search_dashboards", "get_dashboard_by_uid", "update_dashboard",
    ]
    assert ops.calls[-1][1]["operations"] == [
        {"op": "replace", "path": "$.description", "value": "latest run"}
    ]
    assert ops.closed is True, "the mcp-grafana subprocess must be reaped after the run"
    assert not [m for m in _messages(store, run.id) if m.startswith("stage_error")]

def test_a_failing_tool_is_reported_to_the_model_and_the_turn_continues(monkeypatch, clip, tmp_path):
    # One MCP call failing must not throw away the steps that already
    # succeeded, and must not fail the run -- but it must still be recorded.
    class _BrokenMcpOps(_FakeOps):
        def call_tool(self, name, arguments):
            self.calls.append((name, arguments))
            raise RuntimeError("mcp-grafana went away")

    ops = _BrokenMcpOps()
    monkeypatch.setattr(crew, "GrafanaOps", lambda *a, **k: ops)
    monkeypatch.setattr(crew, "_push_run_telemetry", lambda store, state, emit: {"ok": True})
    _stub_stages(monkeypatch)

    llm = _ScriptedLlm(script=[
        ("push_run_telemetry", {}),
        ("search_dashboards", {"query": "customs"}),
        ("ensure_dashboards", {}),
        "published, though search_dashboards failed",
    ])

    store = Store(tmp_path / "t.db")
    run = crew.run_clearance(str(clip), ["FR"], store, tmp_path / "work", model=llm)

    assert run.status == "done"
    assert [name for name, _args in ops.calls] == ["search_dashboards", "ensure_dashboards"], (
        "the step after the failing one must still run"
    )
    stage_errors = [m for m in _messages(store, run.id) if m.startswith("stage_error")]
    assert len(stage_errors) == 1 and "search_dashboards failed" in stage_errors[0]

def test_push_run_telemetry_refuses_to_run_twice(monkeypatch, clip, tmp_path):
    # push_timeline re-picks t0 on every call, so a second push would strand
    # the samples the first one wrote.
    ops = _FakeOps()
    monkeypatch.setattr(crew, "GrafanaOps", lambda *a, **k: ops)
    pushes = []
    monkeypatch.setattr(crew.telemetry, "push_timeline", lambda *a, **k: pushes.append("timeline"))
    monkeypatch.setattr(crew.telemetry, "push_status", lambda *a, **k: None)
    monkeypatch.setattr(crew.telemetry, "push_log", lambda *a, **k: None)
    monkeypatch.setattr(crew.telemetry, "annotate", lambda *a, **k: True)
    monkeypatch.setattr(crew.telemetry, "existing_annotation_keys", lambda run: set())
    _stub_stages(monkeypatch)

    llm = _ScriptedLlm(script=[
        ("push_run_telemetry", {}), ("push_run_telemetry", {}), "published",
    ])

    store = Store(tmp_path / "t.db")
    crew.run_clearance(str(clip), ["FR"], store, tmp_path / "work", model=llm)

    assert pushes == ["timeline"], "the second call must be answered, not performed"

# --- pipeline.run still is the stable entry point ---

def test_pipeline_run_delegates_to_the_crew(monkeypatch, clip, tmp_path):
    seen = {}
    def fake_run_clearance(asset_path, markets, store, workdir, **kwargs):
        seen.update(asset_path=asset_path, markets=markets, workdir=workdir)
        return "sentinel-run-record"
    monkeypatch.setattr(crew, "run_clearance", fake_run_clearance)

    store = Store(tmp_path / "t.db")
    result = pipeline.run(str(clip), ["FR", "SA"], store, tmp_path / "work")

    assert result == "sentinel-run-record"
    assert seen["asset_path"] == str(clip)
    assert seen["markets"] == ["FR", "SA"]

# --- Step 3: live end to end through the ADK runner ---

@pytest.mark.live
def test_live_clearance_publishes_to_grafana(tmp_path):
    """One real run of the real crew: Gemini for ingest/analyst/adjudication,
    the Publisher's own LLM turn, real telemetry pushes and real MCP writes.

    Then three assertions, all read back over the Grafana HTTP API rather
    than from anything this process still holds in memory:
      1. customs_risk exists for both markets for this asset,
      2. at least one finding annotation is on this run's mapped clock,
      3. the overview dashboard's description names this run id -- which
         only the agent could have written, over MCP.
    """
    import httpx

    from customs.config import settings
    from customs.store import Store

    # Its own database, like every other live test in this suite. Not
    # settings.db_path: analyst observation ids are deterministic
    # (obs_{shot_id}_{n}) while observations.id is a global PRIMARY KEY, so a
    # second run of the same asset into the same file raises
    # "UNIQUE constraint failed: observations.id". That is a real product bug
    # in store.py/analyst.py, found by this test on its first live outing and
    # written up in the task 13 report; it is not this task's to fix.
    store = Store(tmp_path / "live.db")
    workdir = Path("runs/work")
    workdir.mkdir(parents=True, exist_ok=True)

    run = crew.run_clearance("docs/samples/test_ad.mp4", ["FR", "SA"], store, workdir)
    findings = store.findings(run.id)
    print(f"\nrun {run.id}: status={run.status} findings={len(findings)}")
    for market in run.markets:
        market_findings = [f for f in findings if f.market == market]
        print(f"  {market}: {pipeline.clearance(market_findings)} "
              f"({len(market_findings)} finding(s))")
    for f in findings:
        print(f"    {f.market} {f.rule_id} sev={f.severity} sourced={f.sourced} "
              f"blocked={f.remediation_blocked} [{f.t_start:.1f}-{f.t_end:.1f}]")
    for (_id, _ts, agent, message) in store.events_since(run.id, 0):
        if agent == "publisher" or message.startswith("stage_error"):
            print(f"  [{agent}] {message[:300]}")

    assert run.status == "done"
    stage_errors = [m for (_i, _t, _a, m) in store.events_since(run.id, 0)
                    if m.startswith("stage_error: publisher")]
    assert not stage_errors, f"the publisher did not get through its turn: {stage_errors}"

    run = store.get_run(run.id)
    headers = {"Authorization": f"Bearer {settings.grafana_sa_token}"}
    base = settings.grafana_url.rstrip("/")

    # 1. customs_risk for both markets. Polled: Mimir ingestion is not
    # synchronous with the OTLP push that returned 200 a moment ago.
    deadline = time.monotonic() + 90
    while True:
        resp = httpx.get(
            f"{base}/api/datasources/proxy/uid/grafanacloud-prom/api/v1/query_range",
            params={"query": 'customs_risk{asset="test_ad"}',
                    "start": f"{run.t0:.3f}", "end": f"{run.t0 + 120:.3f}", "step": "1s"},
            headers=headers, timeout=60.0,
        )
        assert resp.status_code == 200, resp.text[:300]
        series = resp.json().get("data", {}).get("result", [])
        markets_seen = {s["metric"].get("market") for s in series}
        if {"FR", "SA"} <= markets_seen or time.monotonic() > deadline:
            break
        time.sleep(5)
    print(f"customs_risk series: {len(series)}, markets={sorted(m for m in markets_seen if m)}")
    for s in series[:6]:
        print(f"  {s['metric']} -> {len(s['values'])} point(s), max={max(float(v[1]) for v in s['values'])}")
    assert {"FR", "SA"} <= markets_seen, f"missing a market's risk series: {markets_seen}"

    # 2. at least one annotation on this run's clock
    resp = httpx.get(
        f"{base}/api/annotations",
        params={"tags": ["customs", "test_ad"], "from": int(run.t0 * 1000),
                "to": int((run.t0 + 3600) * 1000), "limit": 500},
        headers=headers, timeout=60.0,
    )
    assert resp.status_code == 200, resp.text[:300]
    annotations = resp.json()
    print(f"annotations on this run's clock: {len(annotations)}")
    if annotations:
        print(f"  first: tags={annotations[0]['tags']} text={annotations[0]['text'][:120]}")
    assert annotations, "no finding annotation reached Grafana"

    # 3. the agent's own MCP write
    resp = httpx.get(f"{base}/api/dashboards/uid/customs-overview",
                     headers=headers, timeout=60.0)
    assert resp.status_code == 200, resp.text[:300]
    description = resp.json()["dashboard"].get("description", "")
    print(f"overview description: {description}")
    assert run.id in description, (
        f"the publisher did not write this run into the overview description: {description!r}"
    )
