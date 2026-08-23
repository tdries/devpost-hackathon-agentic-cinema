import subprocess

import pytest

from customs import pipeline
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
