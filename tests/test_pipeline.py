import functools
import subprocess

import pytest

from customs import crew, pipeline
from customs.media import Shot
from customs.schema import Finding, Observation
from customs.store import Store

# --- fixtures ---

@pytest.fixture(scope="session")
def clip(tmp_path_factory):
    # two visually distinct halves so detect_shots finds one real cut, plus a
    # (silent) audio track so extract_audio_span has something to slice --
    # same recipe as test_media.py's clip fixture, offline (no network).
    p = tmp_path_factory.mktemp("pl") / "clip.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi", "-i", "color=red:s=320x240:d=2",
        "-f", "lavfi", "-i", "color=blue:s=320x240:d=2",
        "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono",
        "-filter_complex", "[0:v][1:v]concat=n=2:v=1[v]",
        "-map", "[v]", "-map", "2:a", "-t", "4", "-pix_fmt", "yuv420p", str(p)],
        check=True, capture_output=True, timeout=60)
    return p

@pytest.fixture(autouse=True)
def no_publisher(monkeypatch):
    """Every test in this file is offline and about the clearance stages.

    Task 13 moved pipeline.run onto the ADK crew, whose Publisher stage makes
    a real Gemini call and real Grafana Cloud pushes, so it is switched off at
    the crew's own seam here rather than left to be accidentally exercised by
    a unit test. tests/test_crew.py owns the Publisher's tests, including the
    one proving a dead Grafana never fails a run.
    """
    monkeypatch.setattr(
        crew, "run_clearance", functools.partial(crew.run_clearance, publish=False)
    )

@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    # every stage-error test drives at least one real 3-attempt retry loop;
    # this keeps the whole suite fast without weakening what it proves.
    monkeypatch.setattr(pipeline.time, "sleep", lambda seconds: None)

# --- helpers ---

def _canned_observation(shot_id: str, i: int = 0) -> Observation:
    return Observation(
        id=f"obs_{shot_id}_{i:03d}", shot_id=shot_id, t_start=0.0, t_end=2.0,
        dimension="alcohol_tobacco_drugs", statement="A wine glass is visible.",
        evidence_frame="f.jpg", confidence=0.9,
    )

def _canned_finding(market: str, rule_id: str = "ZZ-01", run_id: str = "run_1") -> Finding:
    # run_id defaults to a placeholder for standalone tests (e.g.
    # test_apply_guard_is_identity) that never touch a Store; tests that
    # persist through pipeline.run must pass the real run_id explicitly,
    # exactly as the real judge() always does, or store.findings(run_id=...)
    # legitimately finds nothing.
    return Finding(
        id=f"fnd_{market}_{rule_id}", run_id=run_id, observation_id="obs_shot_0_000",
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

def _fake_generate_json_transcribe(model, parts, schema):
    return {"transcript": "Twice the energy of any other drink."}

# --- _call_with_retries: the pipeline-level retry primitive ---

def test_call_with_retries_succeeds_first_try():
    calls = []
    def fn():
        calls.append(1)
        return "ok"
    ok, result = pipeline._call_with_retries(fn)
    assert (ok, result) == (True, "ok")
    assert len(calls) == 1

def test_call_with_retries_succeeds_after_transient_failures():
    calls = []
    def fn():
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("transient")
        return "ok"
    ok, result = pipeline._call_with_retries(fn)
    assert (ok, result) == (True, "ok")
    assert len(calls) == 3

def test_call_with_retries_gives_up_after_max_attempts():
    calls = []
    def fn():
        calls.append(1)
        raise RuntimeError("persistent")
    ok, result = pipeline._call_with_retries(fn)
    assert ok is False
    assert isinstance(result, RuntimeError)
    assert len(calls) == pipeline._MAX_ATTEMPTS == 3

# --- apply_guard: delegates to guard.apply (Task 9 placeholder swapped by Task 10) ---
# The rule logic itself (protected_basis blocking, offence-never-remediable,
# passthrough) is guard.py's own responsibility and is covered exhaustively
# in tests/test_guard.py; this only proves the pipeline wires findings and
# pack through to guard.apply and returns exactly what it returns.

def test_apply_guard_delegates_to_guard_apply(monkeypatch):
    findings = [_canned_finding("FR"), _canned_finding("SA")]
    pack = object()  # opaque sentinel: apply_guard must forward it untouched, not inspect it
    sentinel_result = [_canned_finding("US")]

    captured = {}
    def fake_apply(passed_findings, passed_pack):
        captured["findings"] = passed_findings
        captured["pack"] = passed_pack
        return sentinel_result
    monkeypatch.setattr(pipeline.guard, "apply", fake_apply)

    result = pipeline.apply_guard(findings, pack)

    assert captured["findings"] == findings
    assert captured["pack"] is pack
    assert result == sentinel_result

# --- _transcribe_shot: malformed-shape hygiene (review fix round 1) ---

def test_transcribe_shot_returns_the_transcript_when_well_formed(monkeypatch, clip, tmp_path):
    # known-good baseline the two malformed-shape tests below are read against.
    monkeypatch.setattr(pipeline, "generate_json", lambda model, parts, schema: {"transcript": "hello"})
    shot = Shot(shot_id="shot_0", t_start=0.0, t_end=2.0)
    assert pipeline._transcribe_shot(str(clip), shot, tmp_path) == "hello"

def test_transcribe_shot_returns_empty_string_when_raw_is_not_a_dict(monkeypatch, clip, tmp_path):
    # mirrors the same shape-hygiene precedent as analyst.observe_shot /
    # adjudicate.judge: a non-dict response never crashes, it degrades to
    # "no speech" rather than raising out of the retry wrapper.
    monkeypatch.setattr(pipeline, "generate_json", lambda model, parts, schema: ["not", "a", "dict"])
    shot = Shot(shot_id="shot_0", t_start=0.0, t_end=2.0)
    assert pipeline._transcribe_shot(str(clip), shot, tmp_path) == ""

def test_transcribe_shot_returns_empty_string_when_transcript_field_is_not_a_string(monkeypatch, clip, tmp_path):
    # explicit JSON null survives the response_schema's declared "string"
    # type (same precedent as analyst.py's statement/confidence fields), so
    # it must be coalesced by hand rather than returned as the literal "None".
    monkeypatch.setattr(pipeline, "generate_json", lambda model, parts, schema: {"transcript": None})
    shot = Shot(shot_id="shot_0", t_start=0.0, t_end=2.0)
    assert pipeline._transcribe_shot(str(clip), shot, tmp_path) == ""

# --- Step 1/2: pipeline.run, canned analyst + adjudicate + transcription ---

def test_run_persists_observations_and_findings_and_ends_done(monkeypatch, clip, tmp_path):
    monkeypatch.setattr(pipeline, "generate_json", _fake_generate_json_transcribe)
    monkeypatch.setattr(pipeline, "observe_shot", _fake_observe_shot)

    def fake_judge(run_id, observations, pack, on_event=None):
        if on_event:
            on_event("adjudicator", f"judge -> {pack.market} ({len(observations)} candidates)")
        return [_canned_finding(pack.market, run_id=run_id)]
    monkeypatch.setattr(pipeline, "judge", fake_judge)

    store = Store(tmp_path / "t.db")
    run = pipeline.run(str(clip), ["FR", "SA"], store, tmp_path / "work")

    assert run.status == "done"
    assert run.asset_path == str(clip)
    assert run.markets == ["FR", "SA"]

    observations = store.observations(run.id)
    assert len(observations) == 2  # one per shot (clip has 2 shots)
    assert {o.shot_id for o in observations} == {"shot_0", "shot_1"}

    findings = store.findings(run.id)
    assert len(findings) == 2
    assert {f.market for f in findings} == {"FR", "SA"}
    assert all(f.run_id == run.id for f in findings), "judge()'s run_id must be persisted, not the canned placeholder"

    fr_findings = store.findings(run.id, market="FR")
    assert [f.rule_id for f in fr_findings] == ["ZZ-01"]

def test_run_applies_guard_protected_rule_blocks_remediation(monkeypatch, clip, tmp_path):
    # Pipeline-level proof that Task 10's wiring is real, not just unit-tested
    # in isolation: the real SA pack loaded from markets/SA.yaml (load_packs()
    # is never mocked here) carries SA-LGBT-01 with protected_basis: true, the
    # same rule the milestone-1 live gate run actually fired. A canned judge()
    # finding against that real rule_id must come out of the persisted store
    # with remediation_blocked=True, proving pipeline.run's apply_guard(result,
    # pack) call reaches the real guard.apply with the real pack, not a stub.
    monkeypatch.setattr(pipeline, "generate_json", _fake_generate_json_transcribe)
    monkeypatch.setattr(pipeline, "observe_shot", _fake_observe_shot)
    monkeypatch.setattr(
        pipeline, "judge",
        lambda run_id, observations, pack, on_event=None: [
            _canned_finding(pack.market, rule_id="SA-LGBT-01", run_id=run_id)
        ],
    )

    store = Store(tmp_path / "t.db")
    run = pipeline.run(str(clip), ["SA"], store, tmp_path / "work")

    assert run.status == "done"
    findings = store.findings(run.id, market="SA")
    assert len(findings) == 1
    f = findings[0]
    assert f.remediation_blocked is True
    assert f.blocked_reason == "rule basis targets a protected characteristic; human decision required"
    assert f.status == "open", "guard blocking remediation must never change status"

def test_run_threads_transcripts_from_generate_json_into_observe_shot(monkeypatch, clip, tmp_path):
    monkeypatch.setattr(pipeline, "generate_json", _fake_generate_json_transcribe)
    monkeypatch.setattr(pipeline, "judge", lambda *a, **k: [])

    captured = []
    def capturing_observe_shot(video_path, shot, workdir, on_event=None, transcripts=None):
        captured.append(dict(transcripts or {}))
        return []
    monkeypatch.setattr(pipeline, "observe_shot", capturing_observe_shot)

    store = Store(tmp_path / "t.db")
    pipeline.run(str(clip), ["FR"], store, tmp_path / "work")

    assert captured, "observe_shot was never called"
    for transcripts in captured:
        assert transcripts.get("shot_0") == "Twice the energy of any other drink."
        assert transcripts.get("shot_1") == "Twice the energy of any other drink."

def test_run_emits_mission_events_at_each_stage(monkeypatch, clip, tmp_path):
    monkeypatch.setattr(pipeline, "generate_json", _fake_generate_json_transcribe)
    monkeypatch.setattr(pipeline, "observe_shot", _fake_observe_shot)
    monkeypatch.setattr(pipeline, "judge", lambda run_id, observations, pack, on_event=None: [_canned_finding(pack.market)])

    store = Store(tmp_path / "t.db")
    run = pipeline.run(str(clip), ["FR"], store, tmp_path / "work")

    events = store.events_since(run.id, 0)
    messages = [msg for (_id, _ts, _agent, msg) in events]
    joined = "\n".join(messages)
    assert "ingest" in joined
    assert "shot_0" in joined and "shot_1" in joined
    assert "FR" in joined
    assert any("done" in m for m in messages)

# --- Step 3: stage errors never kill the run ---

def test_run_stage_error_one_market_fails_others_still_complete(monkeypatch, clip, tmp_path):
    monkeypatch.setattr(pipeline, "generate_json", _fake_generate_json_transcribe)
    monkeypatch.setattr(pipeline, "observe_shot", _fake_observe_shot)

    judge_calls = {"FR": 0, "SA": 0, "US": 0}
    def flaky_judge(run_id, observations, pack, on_event=None):
        judge_calls[pack.market] += 1
        if pack.market == "SA":
            raise RuntimeError("simulated 5xx")
        return [_canned_finding(pack.market, run_id=run_id)]
    monkeypatch.setattr(pipeline, "judge", flaky_judge)

    store = Store(tmp_path / "t.db")
    run = pipeline.run(str(clip), ["FR", "SA", "US"], store, tmp_path / "work")

    assert run.status == "done", "one market's persistent failure must never take down the whole run"
    assert judge_calls["SA"] == pipeline._MAX_ATTEMPTS == 3, "must retry the failing market 3 times before giving up"
    assert judge_calls["FR"] == 1 and judge_calls["US"] == 1, "healthy markets must not be retried"

    assert store.findings(run.id, market="SA") == []
    assert [f.rule_id for f in store.findings(run.id, market="FR")] == ["ZZ-01"]
    assert [f.rule_id for f in store.findings(run.id, market="US")] == ["ZZ-01"]

    events = store.events_since(run.id, 0)
    stage_errors = [msg for (_id, _ts, agent, msg) in events if "stage_error" in msg]
    assert len(stage_errors) == 1, f"expected exactly one stage_error event, got: {stage_errors}"
    assert "SA" in stage_errors[0]

def test_run_records_stage_error_for_unknown_market_pack(monkeypatch, clip, tmp_path):
    monkeypatch.setattr(pipeline, "generate_json", _fake_generate_json_transcribe)
    monkeypatch.setattr(pipeline, "observe_shot", _fake_observe_shot)
    monkeypatch.setattr(pipeline, "judge", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("must not call judge for a market with no loaded pack")))

    store = Store(tmp_path / "t.db")
    run = pipeline.run(str(clip), ["ZZ"], store, tmp_path / "work")

    assert run.status == "done"
    assert store.findings(run.id) == []
    events = store.events_since(run.id, 0)
    assert any("stage_error" in msg and "ZZ" in msg for (_id, _ts, _agent, msg) in events)

def test_run_stage_error_shot_transcription_failure_still_analyzes_shot(monkeypatch, clip, tmp_path):
    # transcription failing for one shot must not stop that shot from being
    # observed -- it just falls back to the "not available" placeholder
    # (analyst.py's own default), same as any other missing transcript.
    calls = {"n": 0}
    def flaky_transcribe(model, parts, schema):
        calls["n"] += 1
        raise RuntimeError("simulated transcription failure")
    monkeypatch.setattr(pipeline, "generate_json", flaky_transcribe)
    monkeypatch.setattr(pipeline, "observe_shot", _fake_observe_shot)
    monkeypatch.setattr(pipeline, "judge", lambda *a, **k: [])

    store = Store(tmp_path / "t.db")
    run = pipeline.run(str(clip), ["FR"], store, tmp_path / "work")

    assert run.status == "done"
    assert len(store.observations(run.id)) == 2, "both shots must still be observed despite transcription failing"
    events = store.events_since(run.id, 0)
    stage_errors = [msg for (_id, _ts, agent, msg) in events if "stage_error" in msg]
    assert len(stage_errors) == 2, f"expected one stage_error per shot's failed transcription, got: {stage_errors}"

# --- errored_markets: "never evaluated" must never look like "cleared" (review fix round 1) ---

def test_errored_markets_includes_market_with_missing_pack(monkeypatch, clip, tmp_path):
    monkeypatch.setattr(pipeline, "generate_json", _fake_generate_json_transcribe)
    monkeypatch.setattr(pipeline, "observe_shot", _fake_observe_shot)
    monkeypatch.setattr(pipeline, "judge", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("must not call judge for a market with no loaded pack")))

    store = Store(tmp_path / "t.db")
    run = pipeline.run(str(clip), ["ZZ"], store, tmp_path / "work")

    assert pipeline.errored_markets(store, run.id) == {"ZZ"}

def test_errored_markets_includes_market_whose_judge_exhausts_retries(monkeypatch, clip, tmp_path):
    monkeypatch.setattr(pipeline, "generate_json", _fake_generate_json_transcribe)
    monkeypatch.setattr(pipeline, "observe_shot", _fake_observe_shot)

    def flaky_judge(run_id, observations, pack, on_event=None):
        if pack.market == "SA":
            raise RuntimeError("simulated 5xx")
        return [_canned_finding(pack.market, run_id=run_id)]
    monkeypatch.setattr(pipeline, "judge", flaky_judge)

    store = Store(tmp_path / "t.db")
    run = pipeline.run(str(clip), ["FR", "SA"], store, tmp_path / "work")

    assert pipeline.errored_markets(store, run.id) == {"SA"}
    assert "FR" not in pipeline.errored_markets(store, run.id), "a healthy market must never be reported as errored"

def test_errored_markets_excludes_a_market_evaluated_clean(monkeypatch, clip, tmp_path):
    # the exact distinction the review flagged: a market that was genuinely
    # judged and came back with zero findings must be provably different
    # from one that was never judged at all, not just "0 findings" in both
    # cases with no way to tell them apart.
    monkeypatch.setattr(pipeline, "generate_json", _fake_generate_json_transcribe)
    monkeypatch.setattr(pipeline, "observe_shot", _fake_observe_shot)
    monkeypatch.setattr(pipeline, "judge", lambda run_id, observations, pack, on_event=None: [])

    store = Store(tmp_path / "t.db")
    run = pipeline.run(str(clip), ["US"], store, tmp_path / "work")

    assert store.findings(run.id, market="US") == [], "sanity check: this market genuinely has zero findings"
    assert pipeline.errored_markets(store, run.id) == set(), (
        "a market that was actually evaluated and found clean must not appear in errored_markets"
    )

def test_errored_markets_ignores_shot_level_stage_errors(monkeypatch, clip, tmp_path):
    # a shot's transcription/analyst call failing thins the evidence every
    # market sees; it does not mean any market itself went unevaluated, so
    # it must never be mistaken for a market-level error.
    monkeypatch.setattr(pipeline, "generate_json", lambda model, parts, schema: (_ for _ in ()).throw(
        RuntimeError("simulated transcription failure")))
    monkeypatch.setattr(pipeline, "observe_shot", _fake_observe_shot)
    monkeypatch.setattr(pipeline, "judge", lambda run_id, observations, pack, on_event=None: [])

    store = Store(tmp_path / "t.db")
    run = pipeline.run(str(clip), ["FR"], store, tmp_path / "work")

    events = store.events_since(run.id, 0)
    assert any("stage_error" in msg for (_id, _ts, agent, msg) in events), "sanity check: shots did stage_error"
    assert pipeline.errored_markets(store, run.id) == set()


# --- task 14: the deterministic photosensitivity observation ---

@pytest.fixture(scope="session")
def strobe_clip(tmp_path_factory):
    """One shot carrying a 6 flashes/second full-frame white strobe, plus a
    silent audio track so the ingest stage's per-shot extraction has
    something to slice. Built offline from lavfi (never from the sample
    asset), same recipe as tests/test_media.py's fixture."""
    p = tmp_path_factory.mktemp("pl_strobe") / "strobe.mp4"
    subprocess.run([
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", "color=c=0x102030:s=320x240:r=24:d=3",
        "-f", "lavfi", "-i", "color=c=white:s=320x240:r=24:d=3",
        "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono",
        "-filter_complex",
        "[0:v][1:v]overlay=enable='between(t,1.0,2.5)*lt(mod(floor(t*24),4),2)'"
        ",format=yuv420p[v]",
        "-map", "[v]", "-map", "2:a", "-t", "3", str(p)],
        check=True, capture_output=True, timeout=60)
    return p

def test_flash_observations_emits_only_windows_over_the_threshold(monkeypatch):
    from customs.media import FlashWindow
    monkeypatch.setattr(pipeline, "detect_flashes", lambda path: [
        FlashWindow(t_start=30.75, t_end=32.08, flashes_per_second=6.0),
        FlashWindow(t_start=44.0, t_end=46.0, flashes_per_second=2.0),
    ])
    shots = [Shot("shot_4", 28.0, 35.0), Shot("shot_6", 42.0, 49.0)]

    observations = pipeline.flash_observations("asset.mp4", shots)

    assert len(observations) == 1, "2.0 flashes/second is under the 3/s threshold"
    obs = observations[0]
    assert obs.dimension == "photosensitivity_sensory"
    assert obs.confidence == 1.0
    assert obs.shot_id == "shot_4", "attributed to the shot the window starts in"
    assert obs.statement == (
        "Full-frame luminance flashing measured at 6.0 flashes per second "
        "between 30.75s and 32.08s"
    )

def test_flash_observations_at_exactly_the_threshold_is_not_reported(monkeypatch):
    from customs.media import FlashWindow
    monkeypatch.setattr(pipeline, "detect_flashes", lambda path: [
        FlashWindow(t_start=1.0, t_end=3.0, flashes_per_second=3.0),
    ])
    assert pipeline.flash_observations("asset.mp4", []) == []

def test_run_persists_the_measured_flash_observation(monkeypatch, strobe_clip, tmp_path):
    # end to end through the real crew: ffmpeg measures the strobe in ingest,
    # the observation reaches the store alongside the analyst's own, and the
    # adjudicator sees it as a candidate like any other observation.
    monkeypatch.setattr(pipeline, "generate_json", _fake_generate_json_transcribe)
    monkeypatch.setattr(pipeline, "observe_shot", _fake_observe_shot)
    judged = []

    def fake_judge(run_id, observations, pack, on_event=None):
        judged.append([o.dimension for o in observations])
        return []
    monkeypatch.setattr(pipeline, "judge", fake_judge)

    store = Store(tmp_path / "t.db")
    run = pipeline.run(str(strobe_clip), ["US"], store, tmp_path / "work")

    flashes = [
        o for o in store.observations(run.id)
        if o.dimension == "photosensitivity_sensory"
    ]
    assert len(flashes) == 1, store.observations(run.id)
    assert "flashes per second" in flashes[0].statement
    assert flashes[0].confidence == 1.0
    assert "photosensitivity_sensory" in judged[0], "the adjudicator must see it"

def test_run_flash_detection_failure_is_a_stage_error_not_a_dead_run(monkeypatch, clip, tmp_path):
    monkeypatch.setattr(pipeline, "generate_json", _fake_generate_json_transcribe)
    monkeypatch.setattr(pipeline, "observe_shot", _fake_observe_shot)
    monkeypatch.setattr(pipeline, "judge", lambda run_id, obs, pack, on_event=None: [])

    def boom(asset_path, shots):
        raise RuntimeError("ffmpeg is not here")
    monkeypatch.setattr(pipeline, "flash_observations", boom)

    store = Store(tmp_path / "t.db")
    run = pipeline.run(str(clip), ["US"], store, tmp_path / "work")

    assert run.status == "done"
    messages = [m for (_i, _t, _a, m) in store.events_since(run.id, 0)]
    assert any("stage_error: flash detection" in m for m in messages), messages


# --- judge_more: a second market over observations that already exist ----

def test_a_second_market_reuses_the_observations_and_never_reopens_the_asset(
        tmp_path, monkeypatch):
    """BE first, then FR, without touching the film again.

    The expensive half of a run describes what is on screen and no market
    chooses it, so a second jurisdiction is a judging pass. This asserts the
    saving is real: nothing that opens, cuts or looks at the asset may run.
    """
    store = Store(tmp_path / "s.db")
    run = store.create_run(asset_path=str(tmp_path / "ad.mp4"), markets=["BE"])
    store.set_run_t0(run.id, 1_700_000_000.0)
    store.add_observations(run.id, [Observation(
        id="obs_shot_0_000", shot_id="shot_0", t_start=0.0, t_end=2.0,
        dimension="alcohol_tobacco_drugs", statement="A glass of red wine.",
        evidence_frame="", confidence=0.9)])

    for name in ("detect_shots", "extract_keyframes", "extract_audio_span"):
        monkeypatch.setattr(f"customs.media.{name}", _boom(name), raising=False)
    monkeypatch.setattr(pipeline, "observe_shot", _boom("observe_shot"))
    monkeypatch.setattr("customs.telemetry.extend_timeline", lambda *a, **k: None)
    monkeypatch.setattr("customs.telemetry.push_status", lambda *a, **k: None)
    monkeypatch.setattr("customs.telemetry.push_log", lambda *a, **k: None)

    seen = {}
    def fake_judge(run_id, observations, pack, on_event=None):
        seen[pack.market] = [o.id for o in observations]
        return []
    monkeypatch.setattr(pipeline, "judge", fake_judge)

    fresh = store.add_run_markets(run.id, ["FR", "BE"])
    assert fresh == ["FR"]  # BE was already judged; it is not judged twice
    clearances = pipeline.judge_more(store, store.get_run(run.id), fresh, 2.0)

    # the French judge saw the Belgian run's observations, unchanged
    assert seen == {"FR": ["obs_shot_0_000"]}
    assert clearances == {"FR": "cleared"}
    assert store.get_run(run.id).markets == ["BE", "FR"]


def _boom(name):
    def explode(*a, **k):
        raise AssertionError(f"{name} ran: a second market must not re-ingest")
    return explode


def test_judge_more_refuses_a_run_it_cannot_reuse(tmp_path):
    """No observations means there is nothing to save; say so, do not guess."""
    store = Store(tmp_path / "s.db")
    run = store.create_run(asset_path=str(tmp_path / "ad.mp4"), markets=["BE"])
    with pytest.raises(ValueError, match="no observations"):
        pipeline.judge_more(store, run, ["FR"], 2.0)
