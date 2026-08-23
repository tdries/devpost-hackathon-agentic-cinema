"""Verifier tests: the loop back onto the analyst, offline.

The analyst and adjudicator are faked at the pipeline seam (pipeline.observe_shot,
pipeline.judge), which is the same seam tests/test_pipeline.py steers a whole
run with, and telemetry is faked at telemetry._post/_get. Everything else --
the store writes, the status transitions, the clearance recomputation -- is
real.
"""
import subprocess

import pytest

from customs import pipeline, remediate, telemetry, verify
from customs.schema import ChangeRecord, Finding, Observation
from customs.store import Store

@pytest.fixture(scope="session")
def clip(tmp_path_factory):
    p = tmp_path_factory.mktemp("vfy") / "clip.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi", "-i", "color=red:s=320x240:d=2",
        "-f", "lavfi", "-i", "color=blue:s=320x240:d=2",
        "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono",
        "-filter_complex", "[0:v][1:v]concat=n=2:v=1[v]",
        "-map", "[v]", "-map", "2:a", "-t", "4", "-pix_fmt", "yuv420p", str(p)],
        check=True, capture_output=True, timeout=60)
    return p

def _finding(**overrides):
    fields = dict(
        id="fnd_FR_FR-LANG-01_obs_shot_0_000", run_id="run_1",
        observation_id="obs_shot_0_000", market="FR", rule_id="FR-LANG-01",
        klass="legal", severity=95, t_start=0.5, t_end=1.5,
        rationale="English on-screen text", citation_ref="Loi Toubon",
        citation_url="https://example.org/toubon", sourced=True, remediable=True,
        remediation_blocked=False, blocked_reason="", status="remediating",
    )
    fields.update(overrides)
    return Finding(**fields)

def _observation(**overrides):
    fields = dict(
        id="obs_shot_0_000", shot_id="shot_0", t_start=0.0, t_end=2.0,
        dimension="text_legibility", statement="On-screen English text.",
        evidence_frame="", confidence=0.9,
    )
    fields.update(overrides)
    return Observation(**fields)

@pytest.fixture
def remediated(tmp_path, clip):
    """A run whose FR master has already been edited, with one finding at
    status remediating and its ChangeRecord persisted."""
    store = Store(tmp_path / "customs.db")
    run = store.create_run(asset_path=str(clip), markets=["FR"])
    store.add_observations(run.id, [
        _observation(),
        _observation(id="obs_shot_1_000", shot_id="shot_1", t_start=2.0, t_end=4.0),
    ])
    finding = _finding(run_id=run.id)
    store.add_findings([finding])
    master = remediate.localized_master(run, "FR", store)
    master.parent.mkdir(parents=True, exist_ok=True)
    master.write_bytes(clip.read_bytes())
    change = ChangeRecord(id="chg_1", run_id=run.id, finding_id=finding.id,
                          method="relettering", description="relettered",
                          before_frame="before.png", after_frame="after.png")
    store.add_change(change)
    return store, run, finding, change, master

@pytest.fixture
def no_telemetry(monkeypatch):
    """Capture telemetry instead of posting it: (kind, payload) per call."""
    calls = []
    monkeypatch.setattr(telemetry, "push_status",
                        lambda run, market, clearance, findings: calls.append(("status", market, clearance)))
    monkeypatch.setattr(telemetry, "annotate_resolution",
                        lambda run, change, store=None: calls.append(("resolution", change.id)))
    return calls

@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    # the two failure tests below drive a real 3-attempt retry loop each
    monkeypatch.setattr(pipeline.time, "sleep", lambda seconds: None)

def _fake_judge_returning(findings):
    def fake_judge(run_id, observations, pack, on_event=None):
        return list(findings)
    return fake_judge

def test_confirm_resolves_the_finding_when_the_rule_no_longer_fires(
        remediated, monkeypatch, no_telemetry, tmp_path):
    store, run, finding, change, master = remediated
    seen = []

    def fake_observe(video_path, shot, workdir, on_event=None, transcripts=None):
        seen.append((str(video_path), shot.shot_id))
        return [_observation(dimension="food_and_animals", statement="A glass of tea.")]
    monkeypatch.setattr(pipeline, "observe_shot", fake_observe)
    monkeypatch.setattr(pipeline, "judge", _fake_judge_returning([]))

    assert verify.confirm(run, "FR", [change], store, tmp_path / "work") is True

    assert store.findings(run.id, "FR")[0].status == "resolved"
    assert seen == [(str(master), "shot_0")], "only the touched shot, on the localized master"
    assert ("status", "FR", "cleared") in no_telemetry
    assert ("resolution", "chg_1") in no_telemetry

def test_confirm_reopens_the_finding_when_the_same_rule_still_fires(
        remediated, monkeypatch, no_telemetry, tmp_path):
    store, run, finding, change, _master = remediated
    monkeypatch.setattr(pipeline, "observe_shot",
                        lambda *a, **k: [_observation()])
    still_there = _finding(id="fnd_new", run_id=run.id, t_start=0.6, t_end=1.4, status="open")
    monkeypatch.setattr(pipeline, "judge", _fake_judge_returning([still_there]))

    assert verify.confirm(run, "FR", [change], store, tmp_path / "work") is False

    assert store.findings(run.id, "FR")[0].status == "open"
    assert ("status", "FR", "blocked") in no_telemetry, "an unfixed market stays blocked"
    assert not any(kind == "resolution" for kind, *_rest in no_telemetry)

def test_confirm_ignores_a_surviving_finding_of_a_different_rule(
        remediated, monkeypatch, no_telemetry, tmp_path):
    # the verifier asks "is THIS violation gone", not "is the shot spotless":
    # a different rule firing on the edited shot is a new finding for the next
    # clearance run, not evidence that this fix failed.
    store, run, finding, change, _master = remediated
    monkeypatch.setattr(pipeline, "observe_shot", lambda *a, **k: [_observation()])
    other = _finding(id="fnd_other", run_id=run.id, rule_id="FR-ALC-01", status="open")
    monkeypatch.setattr(pipeline, "judge", _fake_judge_returning([other]))

    assert verify.confirm(run, "FR", [change], store, tmp_path / "work") is True
    assert store.findings(run.id, "FR")[0].status == "resolved"

def test_confirm_ignores_the_same_rule_at_a_different_timecode(
        remediated, monkeypatch, no_telemetry, tmp_path):
    store, run, finding, change, _master = remediated
    monkeypatch.setattr(pipeline, "observe_shot", lambda *a, **k: [_observation()])
    elsewhere = _finding(id="fnd_late", run_id=run.id, t_start=3.0, t_end=3.9, status="open")
    monkeypatch.setattr(pipeline, "judge", _fake_judge_returning([elsewhere]))

    assert verify.confirm(run, "FR", [change], store, tmp_path / "work") is True

def test_confirm_never_persists_the_re_observation(
        remediated, monkeypatch, no_telemetry, tmp_path):
    store, run, _finding, change, _master = remediated
    before = len(store.observations(run.id))
    monkeypatch.setattr(pipeline, "observe_shot", lambda *a, **k: [_observation()])
    monkeypatch.setattr(pipeline, "judge", _fake_judge_returning([]))

    verify.confirm(run, "FR", [change], store, tmp_path / "work")

    assert len(store.observations(run.id)) == before
    assert len(store.findings(run.id, "FR")) == 1

def test_confirm_reports_false_for_a_change_naming_an_unknown_finding(
        remediated, monkeypatch, no_telemetry, tmp_path):
    store, run, _finding, _change, _master = remediated

    def fail(*a, **k):
        raise AssertionError("must not re-observe anything for an unknown finding")
    monkeypatch.setattr(pipeline, "observe_shot", fail)
    forged = ChangeRecord(id="chg_x", run_id=run.id, finding_id="fnd_does_not_exist",
                          method="relettering", description="", before_frame="",
                          after_frame="")

    assert verify.confirm(run, "FR", [forged], store, tmp_path / "work") is False
    messages = [m for (_i, _t, _a, m) in store.events_since(run.id, 0)]
    assert any("stage_error: verify" in m for m in messages), messages

def test_confirm_reports_false_when_there_is_no_localized_master(tmp_path, clip):
    store = Store(tmp_path / "customs.db")
    run = store.create_run(asset_path=str(clip), markets=["FR"])
    change = ChangeRecord(id="c", run_id=run.id, finding_id="f", method="reframe",
                          description="", before_frame="", after_frame="")
    assert verify.confirm(run, "FR", [change], store, tmp_path / "work") is False

def test_confirm_survives_a_telemetry_failure(remediated, monkeypatch, tmp_path):
    store, run, _finding, change, _master = remediated
    monkeypatch.setattr(pipeline, "observe_shot", lambda *a, **k: [_observation()])
    monkeypatch.setattr(pipeline, "judge", _fake_judge_returning([]))

    def dead(*a, **k):
        raise RuntimeError("grafana is down")
    monkeypatch.setattr(telemetry, "push_status", dead)

    assert verify.confirm(run, "FR", [change], store, tmp_path / "work") is True
    assert store.findings(run.id, "FR")[0].status == "resolved"
    messages = [m for (_i, _t, _a, m) in store.events_since(run.id, 0)]
    assert any("telemetry push failed" in m for m in messages), messages

def test_confirm_reopens_the_finding_when_the_re_observation_fails(
        remediated, monkeypatch, no_telemetry, tmp_path):
    # a verifier that gave up without doing this would leave the finding at
    # "remediating", where clearance() cannot see it, and the market would
    # report cleared with the violation still in the master.
    store, run, _finding, change, _master = remediated

    def boom(*a, **k):
        raise RuntimeError("the vision model is down")
    monkeypatch.setattr(pipeline, "observe_shot", boom)

    assert verify.confirm(run, "FR", [change], store, tmp_path / "work") is False
    assert store.findings(run.id, "FR")[0].status == "open"

def test_confirm_reopens_the_finding_when_re_adjudication_fails(
        remediated, monkeypatch, no_telemetry, tmp_path):
    store, run, _finding, change, _master = remediated
    monkeypatch.setattr(pipeline, "observe_shot", lambda *a, **k: [_observation()])

    def boom(*a, **k):
        raise RuntimeError("the judge is down")
    monkeypatch.setattr(pipeline, "judge", boom)

    assert verify.confirm(run, "FR", [change], store, tmp_path / "work") is False
    assert store.findings(run.id, "FR")[0].status == "open"
