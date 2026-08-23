import subprocess
from pathlib import Path

import pytest

from customs import analyst, media
from customs.media import Shot
from customs.schema import Observation

TEST_AD = Path(__file__).resolve().parents[1] / "docs" / "samples" / "test_ad.mp4"

@pytest.fixture(scope="session")
def clip(tmp_path_factory):
    # short, real, local clip -- offline (no network), mirrors test_media.py's fixture
    p = tmp_path_factory.mktemp("an") / "clip.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi", "-i", "color=red:s=320x240:d=2",
        "-pix_fmt", "yuv420p", str(p)],
        check=True, capture_output=True, timeout=60)
    return p

# --- Step 1/2: offline test, monkeypatching the model boundary only ---

def test_observe_shot_drops_unknown_dimension_and_warns(monkeypatch, clip, tmp_path):
    shot = Shot(shot_id="shot_0", t_start=0.0, t_end=media.probe_duration(clip))
    # the invalid item is FIRST: the surviving observation's id must still
    # read _001, proving ids are the raw response index, not renumbered
    # after a drop.
    canned = [
        {"dimension": "nonsense", "statement": "Not a real taxonomy dimension.", "confidence": 0.5},
        {"dimension": "alcohol_tobacco_drugs", "statement": "A wine glass is visible.", "confidence": 0.9},
    ]
    monkeypatch.setattr(analyst, "generate_json", lambda model, parts, schema: canned)

    events = []
    observations = analyst.observe_shot(
        clip, shot, tmp_path, on_event=lambda agent, msg: events.append((agent, msg))
    )

    assert len(observations) == 1
    obs = observations[0]
    assert isinstance(obs, Observation)
    assert obs.dimension == "alcohol_tobacco_drugs"
    assert obs.shot_id == "shot_0"
    assert obs.t_start == shot.t_start and obs.t_end == shot.t_end
    assert obs.id == f"obs_{shot.shot_id}_001"
    assert obs.evidence_frame == str(tmp_path / "frames" / f"{shot.shot_id}_kf0.png")
    assert obs.confidence == 0.9

    assert any(a == "analyst" for a, _ in events)
    warnings = [m for a, m in events if "warning" in m.lower()]
    assert warnings, f"expected a warning event for the dropped observation, got: {events}"
    assert "nonsense" in warnings[0]

def test_observe_shot_drops_whole_response_when_not_a_list(monkeypatch, clip, tmp_path):
    # the model returns a bare object instead of an array: unusable as a
    # whole, not just one bad item -- must not crash trying to enumerate it.
    bad_raw = {"dimension": "alcohol_tobacco_drugs", "statement": "x", "confidence": 0.5}
    monkeypatch.setattr(analyst, "generate_json", lambda model, parts, schema: bad_raw)
    shot = Shot(shot_id="shot_0", t_start=0.0, t_end=media.probe_duration(clip))

    events = []
    observations = analyst.observe_shot(
        clip, shot, tmp_path, on_event=lambda agent, msg: events.append((agent, msg))
    )

    assert observations == []
    warnings = [m for a, m in events if "warning" in m.lower()]
    assert len(warnings) == 1, f"expected exactly one warning for the whole response, got: {events}"

def test_observe_shot_drops_items_that_are_not_dicts(monkeypatch, clip, tmp_path):
    # a list of strings instead of a list of objects: the top level is a
    # list (fine), but nothing inside it is a dict -- each item drops on
    # its own, with its own warning, rather than crashing on .get().
    monkeypatch.setattr(
        analyst, "generate_json",
        lambda model, parts, schema: ["not a dict", "also not a dict"],
    )
    shot = Shot(shot_id="shot_0", t_start=0.0, t_end=media.probe_duration(clip))

    events = []
    observations = analyst.observe_shot(
        clip, shot, tmp_path, on_event=lambda agent, msg: events.append((agent, msg))
    )

    assert observations == []
    warnings = [m for a, m in events if "warning" in m.lower()]
    assert len(warnings) == 2, f"expected one warning per bad item, got: {events}"

def test_observe_shot_coalesces_null_confidence_and_drops_null_statement(monkeypatch, clip, tmp_path):
    # explicit JSON null survives the response_schema's declared types.
    # item 0's null statement must be coalesced then dropped (empty after
    # coalescing), not stored as the literal string "None". item 1's null
    # confidence must be coalesced to a safe default, not raise TypeError
    # out of float(None).
    canned = [
        {"dimension": "alcohol_tobacco_drugs", "statement": None, "confidence": 0.9},
        {"dimension": "alcohol_tobacco_drugs", "statement": "A wine glass is visible.", "confidence": None},
    ]
    monkeypatch.setattr(analyst, "generate_json", lambda model, parts, schema: canned)
    shot = Shot(shot_id="shot_0", t_start=0.0, t_end=media.probe_duration(clip))

    events = []
    observations = analyst.observe_shot(
        clip, shot, tmp_path, on_event=lambda agent, msg: events.append((agent, msg))
    )

    assert len(observations) == 1
    obs = observations[0]
    assert obs.id == f"obs_{shot.shot_id}_001"
    assert obs.statement == "A wine glass is visible."
    assert obs.confidence == 0.0

    warnings = [m for a, m in events if "warning" in m.lower()]
    assert any("empty statement" in w for w in warnings), f"expected an empty-statement warning, got: {events}"

def test_observe_shot_clamps_confidence_into_0_1(monkeypatch, clip, tmp_path):
    canned = [
        {"dimension": "alcohol_tobacco_drugs", "statement": "over", "confidence": 5.0},
        {"dimension": "alcohol_tobacco_drugs", "statement": "under", "confidence": -3.0},
    ]
    monkeypatch.setattr(analyst, "generate_json", lambda model, parts, schema: canned)
    shot = Shot(shot_id="shot_0", t_start=0.0, t_end=media.probe_duration(clip))

    observations = analyst.observe_shot(clip, shot, tmp_path)

    assert [o.confidence for o in observations] == [1.0, 0.0]

def test_observe_shot_emits_start_event(monkeypatch, clip, tmp_path):
    monkeypatch.setattr(analyst, "generate_json", lambda model, parts, schema: [])
    shot = Shot(shot_id="shot_9", t_start=0.0, t_end=media.probe_duration(clip))
    events = []
    analyst.observe_shot(clip, shot, tmp_path, on_event=lambda agent, msg: events.append((agent, msg)))
    assert any(a == "analyst" and "shot_9" in m for a, m in events)

def test_observe_shot_works_without_on_event(monkeypatch, clip, tmp_path):
    # on_event is optional; must not raise when omitted
    monkeypatch.setattr(analyst, "generate_json", lambda model, parts, schema: [
        {"dimension": "alcohol_tobacco_drugs", "statement": "x", "confidence": 0.4},
    ])
    shot = Shot(shot_id="shot_0", t_start=0.0, t_end=media.probe_duration(clip))
    observations = analyst.observe_shot(clip, shot, tmp_path)
    assert len(observations) == 1

# --- merge-logic unit tests (pure function) ---

def test_merge_micro_shots_merges_short_shot_into_previous():
    shots = [
        Shot("shot_0", 0.0, 2.0),
        Shot("shot_1", 2.0, 2.1),   # 0.1s, too short -> folds into shot_0
        Shot("shot_2", 2.1, 5.0),
    ]
    merged = analyst.merge_micro_shots(shots, min_len=0.5)
    assert [(s.shot_id, s.t_start, s.t_end) for s in merged] == [
        ("shot_0", 0.0, 2.1),
        ("shot_2", 2.1, 5.0),
    ]

def test_merge_micro_shots_folds_a_run_of_consecutive_short_shots():
    shots = [
        Shot("shot_0", 0.0, 2.0),
        Shot("shot_1", 2.0, 2.1),
        Shot("shot_2", 2.1, 2.2),
        Shot("shot_3", 2.2, 5.0),
    ]
    merged = analyst.merge_micro_shots(shots, min_len=0.5)
    assert [(s.shot_id, s.t_start, s.t_end) for s in merged] == [
        ("shot_0", 0.0, 2.2),
        ("shot_3", 2.2, 5.0),
    ]

def test_merge_micro_shots_first_shot_absorbs_forward():
    # the first shot has no previous shot to merge into; if it is itself too
    # short it must absorb the shot(s) after it instead of being dropped.
    shots = [
        Shot("shot_0", 0.0, 0.1),
        Shot("shot_1", 0.1, 0.2),
        Shot("shot_2", 0.2, 0.3),
        Shot("shot_3", 0.3, 10.0),
    ]
    merged = analyst.merge_micro_shots(shots, min_len=0.5)
    assert len(merged) == 1
    assert merged[0].shot_id == "shot_0"
    assert merged[0].t_start == 0.0
    assert merged[0].t_end == 10.0

def test_merge_micro_shots_noop_when_all_shots_are_long_enough():
    shots = [Shot("shot_0", 0.0, 1.0), Shot("shot_1", 1.0, 3.0)]
    merged = analyst.merge_micro_shots(shots, min_len=0.5)
    assert merged == shots

def test_merge_micro_shots_empty_input():
    assert analyst.merge_micro_shots([], min_len=0.5) == []

def test_merge_micro_shots_does_not_mutate_input():
    shots = [Shot("shot_0", 0.0, 0.1), Shot("shot_1", 0.1, 5.0)]
    original = [Shot(s.shot_id, s.t_start, s.t_end) for s in shots]
    analyst.merge_micro_shots(shots, min_len=0.5)
    assert shots == original

# --- observe_all: sequential wiring, the merge event, aggregation ---

def test_observe_all_merges_then_observes_each_shot_in_order(monkeypatch, tmp_path):
    raw_shots = [
        Shot("shot_0", 0.0, 2.0),
        Shot("shot_1", 2.0, 2.1),   # merges into shot_0
        Shot("shot_2", 2.1, 5.0),
    ]
    monkeypatch.setattr(analyst, "detect_shots", lambda path: raw_shots)

    seen = []
    def fake_observe_shot(video_path, shot, workdir, on_event=None):
        seen.append(shot.shot_id)
        return [Observation(
            id=f"obs_{shot.shot_id}_000", shot_id=shot.shot_id,
            t_start=shot.t_start, t_end=shot.t_end,
            dimension="alcohol_tobacco_drugs", statement="x",
            evidence_frame="f.jpg", confidence=0.5,
        )]
    monkeypatch.setattr(analyst, "observe_shot", fake_observe_shot)

    events = []
    observations = analyst.observe_all(
        "video.mp4", tmp_path, on_event=lambda agent, msg: events.append((agent, msg))
    )

    assert seen == ["shot_0", "shot_2"]
    assert [o.shot_id for o in observations] == ["shot_0", "shot_2"]
    merge_events = [m for a, m in events if "merged" in m.lower()]
    assert merge_events, f"expected a merge-count event, got: {events}"
    assert "1" in merge_events[0]

def test_observe_all_reports_zero_merged_when_nothing_short(monkeypatch, tmp_path):
    raw_shots = [Shot("shot_0", 0.0, 2.0), Shot("shot_1", 2.0, 5.0)]
    monkeypatch.setattr(analyst, "detect_shots", lambda path: raw_shots)
    monkeypatch.setattr(analyst, "observe_shot", lambda *a, **k: [])

    events = []
    analyst.observe_all("video.mp4", tmp_path, on_event=lambda agent, msg: events.append((agent, msg)))
    merge_events = [m for a, m in events if "merged" in m.lower()]
    assert merge_events
    assert "0" in merge_events[0]

# --- PROMPT contract ---

def test_prompt_interpolates_sorted_taxonomy():
    from customs.packs import taxonomy
    rendered = analyst.PROMPT.format(taxonomy=sorted(taxonomy()))
    assert "alcohol_tobacco_drugs" in rendered
    assert "{taxonomy}" not in rendered
    assert "neutral shot logger" in rendered
    assert "Do not judge, do not localize, do not filter." in rendered

# --- Step 4: live spot-check ---

@pytest.mark.live
def test_observe_shot_live_wine_toast_shot_flags_alcohol(tmp_path):
    shots = media.detect_shots(TEST_AD)
    shot = shots[0]  # shot 1: terrace cafe wine toast (docs/samples/landmines.yaml)
    observations = analyst.observe_shot(TEST_AD, shot, tmp_path)
    dims = {o.dimension for o in observations}
    assert "alcohol_tobacco_drugs" in dims, f"observed dimensions: {dims}"
