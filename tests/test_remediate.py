"""Remediator tests: the method mapping, the guard re-check, and one edit of
each kind applied to a real (tiny, offline) video.

Every model call is faked at this module's own seams -- _edit_image (the
gemini-3.1-flash-image edit), _compliant_line (the replacement copy) and
_speak (TTS) -- which is the same boundary tests/test_adjudicate.py fakes
generate_json/generate_grounded at. Everything below those seams is real:
real ffmpeg, real store writes, real ChangeRecords.
"""
import subprocess

import pytest

from customs import remediate
from customs.schema import Finding, Observation
from customs.store import Store

# --- fixtures / helpers ---

@pytest.fixture(scope="session")
def clip(tmp_path_factory):
    # one visual cut plus a real (silent) audio track, so both the video edit
    # paths and replace_audio_span have something to work on. Same offline
    # lavfi recipe as tests/test_media.py.
    p = tmp_path_factory.mktemp("rem") / "clip.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi", "-i", "color=red:s=320x240:d=2",
        "-f", "lavfi", "-i", "color=blue:s=320x240:d=2",
        "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono",
        "-filter_complex", "[0:v][1:v]concat=n=2:v=1[v]",
        "-map", "[v]", "-map", "2:a", "-t", "4", "-pix_fmt", "yuv420p", str(p)],
        check=True, capture_output=True, timeout=60)
    return p

@pytest.fixture
def png_bytes(tmp_path_factory):
    """A real PNG, standing in for what the image model hands back. Built at
    a deliberately different resolution from the clip so the fit-to-master
    rescale is exercised rather than accidentally skipped."""
    p = tmp_path_factory.mktemp("edit") / "edited.png"
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi", "-i", "color=green:s=512x512:d=1",
        "-frames:v", "1", str(p)], check=True, capture_output=True, timeout=60)
    return p.read_bytes()

def _finding(**overrides):
    fields = dict(
        id="fnd_FR_FR-LANG-01_obs_shot_1_000", run_id="run_1",
        observation_id="obs_shot_1_000", market="FR", rule_id="FR-LANG-01",
        klass="legal", severity=60, t_start=0.5, t_end=1.5,
        rationale="English on-screen text with no French translation",
        citation_ref="Loi Toubon", citation_url="https://example.org/toubon",
        sourced=True, remediable=True, remediation_blocked=False,
        blocked_reason="", status="open",
    )
    fields.update(overrides)
    return Finding(**fields)

def _observation(**overrides):
    fields = dict(
        id="obs_shot_1_000", shot_id="shot_1", t_start=0.5, t_end=1.5,
        dimension="text_legibility",
        statement="The on-screen text reads 'Happiness is one sip away'.",
        evidence_frame="", confidence=0.9,
    )
    fields.update(overrides)
    return Observation(**fields)

@pytest.fixture
def store_with_run(tmp_path, clip):
    """A store whose db sits in tmp_path, so runs/{run_id}/ artifacts land
    under tmp_path too (remediate.run_dir derives the runs root from the
    store's own path)."""
    store = Store(tmp_path / "customs.db")
    run = store.create_run(asset_path=str(clip), markets=["FR"])
    finding = _finding(run_id=run.id)
    store.add_observations(run.id, [_observation()])
    store.add_findings([finding])
    return store, run, finding

@pytest.fixture
def fake_models(monkeypatch):
    """Fake every model seam remediate.py owns. Records what it was asked."""
    calls = {"edits": [], "lines": [], "speech": []}

    def fake_edit(instruction, image_bytes, mime_type="image/png"):
        calls["edits"].append((instruction, len(image_bytes)))
        return calls["png"]

    def fake_line(finding, market_name):
        calls["lines"].append((finding.rule_id, market_name))
        return "Solstice. A refreshing drink."

    def fake_speak(line):
        calls["speech"].append(line)
        # 0.5s of silence at 24kHz mono s16le, the shape _speak really returns
        return b"\x00\x00" * 12000, 24000

    monkeypatch.setattr(remediate, "_edit_image", fake_edit)
    monkeypatch.setattr(remediate, "_compliant_line", fake_line)
    monkeypatch.setattr(remediate, "_speak", fake_speak)
    return calls

# --- plan: the documented mapping table ---

@pytest.mark.parametrize("dimension,expected", [
    ("text_legibility", "relettering"),
    ("alcohol_tobacco_drugs", "prop_swap"),
    ("food_and_animals", "prop_swap"),
    ("health_claims_pharma", "revoice"),
    ("comparative_claims", "revoice"),
    ("gesture_body_language", "reframe"),
    ("modesty_dress_body", "reframe"),
    ("sexual_orientation_gender_id", "reframe"),
    ("photosensitivity_sensory", "reframe"),
])
def test_plan_maps_dimension_to_method(dimension, expected):
    # a neutral statement, so each row tests the dimension mapping alone and
    # not the on-screen-text refinement the two claims rows get below.
    obs = _observation(dimension=dimension, statement="The element is present in the shot.")
    assert remediate.plan(_finding(), obs) == expected

def test_plan_reletters_a_comparative_claim_that_is_on_screen_text():
    obs = _observation(
        dimension="comparative_claims",
        statement="On-screen text reads 'Twice the energy of any other drink'.",
    )
    assert remediate.plan(_finding(rule_id="FR-CMP-01"), obs) == "relettering"

def test_plan_revoices_a_comparative_claim_that_is_spoken():
    obs = _observation(
        dimension="comparative_claims",
        statement="A voice-over says 'Twice the energy of any other drink'.",
    )
    assert remediate.plan(_finding(rule_id="FR-CMP-01"), obs) == "revoice"

def test_plan_without_an_observation_reads_the_dimension_off_the_market_pack():
    # FR-LANG-01 is text_legibility and FR-ALC-01 is alcohol_tobacco_drugs in
    # the real markets/FR.yaml, so this exercises the pack join, not a stub.
    assert remediate.plan(_finding(rule_id="FR-LANG-01")) == "relettering"
    assert remediate.plan(_finding(rule_id="FR-ALC-01")) == "prop_swap"

def test_plan_falls_back_to_reframe_for_an_unknown_rule():
    assert remediate.plan(_finding(rule_id="ZZ-NOPE-99")) == "reframe"

# --- apply: the guard, enforced a second time ---

@pytest.mark.parametrize("overrides", [
    {"remediation_blocked": True, "blocked_reason": "protected characteristic"},
    {"klass": "offence"},
    {"remediable": False},
])
def test_apply_refuses_what_the_guard_took_off_the_table(overrides, store_with_run,
                                                         tmp_path, fake_models):
    store, run, _ = store_with_run
    finding = _finding(run_id=run.id, **overrides)

    with pytest.raises(remediate.RemediationBlocked):
        remediate.apply(run, finding, "reframe", tmp_path / "work", store)

    assert store.changes(run.id) == []
    assert not remediate.localized_master(run, "FR", store).exists()

def test_apply_rejects_an_unknown_method(store_with_run, tmp_path):
    store, run, finding = store_with_run
    with pytest.raises(ValueError):
        remediate.apply(run, finding, "regenerate_everything", tmp_path / "work", store)

# --- apply: one edit of each kind, on a real video ---

def test_apply_relettering_writes_a_localized_master_and_a_change_record(
        store_with_run, tmp_path, fake_models, png_bytes):
    store, run, finding = store_with_run
    fake_models["png"] = png_bytes

    change = remediate.apply(run, finding, "relettering", tmp_path / "work", store,
                             replacement="Le bonheur est a une gorgee")

    master = remediate.localized_master(run, "FR", store)
    assert master.exists() and master.stat().st_size > 0
    assert master.name == "localized_FR.mp4"
    assert change.method == "relettering"
    assert change.finding_id == finding.id
    from pathlib import Path
    assert Path(change.before_frame).exists()
    assert Path(change.after_frame).exists()
    assert store.changes(run.id) == [change]
    # the edit instruction must carry the requested replacement text
    assert "Le bonheur est a une gorgee" in fake_models["edits"][0][0]
    # the description is what dashboards and annotations show: readable, and
    # not a paragraph of prompt
    assert change.description == 'relettered the on-screen text as "Le bonheur est a une gorgee"'
    # the exact instruction stays recoverable from the run's own event log
    messages = [m for (_i, _t, _a, m) in store.events_since(run.id, 0)]
    assert any(m.startswith("relettering instruction: ") for m in messages), messages

def test_apply_sets_the_finding_to_remediating_never_straight_to_resolved(
        store_with_run, tmp_path, fake_models, png_bytes):
    store, run, finding = store_with_run
    fake_models["png"] = png_bytes

    remediate.apply(run, finding, "relettering", tmp_path / "work", store)

    stored = store.findings(run.id, "FR")[0]
    assert stored.status == "remediating", "only the verifier may write 'resolved'"

def test_apply_emits_mission_events(store_with_run, tmp_path, fake_models, png_bytes):
    store, run, finding = store_with_run
    fake_models["png"] = png_bytes

    remediate.apply(run, finding, "relettering", tmp_path / "work", store)

    messages = [m for (_i, _t, agent, m) in store.events_since(run.id, 0)
                if agent == "remediator"]
    assert any("relettering" in m for m in messages), messages

def test_apply_accumulates_edits_on_the_same_localized_master(
        store_with_run, tmp_path, fake_models, png_bytes, monkeypatch):
    store, run, finding = store_with_run
    fake_models["png"] = png_bytes
    bases = []
    real_overlay = remediate.media.overlay_image

    def spy(path, png, t_start, t_end, out_path):
        bases.append(str(path))
        return real_overlay(path, png, t_start, t_end, out_path)
    monkeypatch.setattr(remediate.media, "overlay_image", spy)

    remediate.apply(run, finding, "relettering", tmp_path / "work", store)
    second = _finding(run_id=run.id, id="fnd_FR_FR-LANG-01_obs_shot_1_001",
                      t_start=2.5, t_end=3.5)
    store.add_findings([second])
    remediate.apply(run, second, "relettering", tmp_path / "work", store)

    master = remediate.localized_master(run, "FR", store)
    assert bases[0] == run.asset_path, "first edit starts from the original asset"
    assert bases[1] == str(master), "later edits accumulate on the localized master"
    assert len(store.changes(run.id)) == 2

def test_apply_reframe_uses_ffmpeg_only_and_calls_no_model(
        store_with_run, tmp_path, monkeypatch):
    store, run, finding = store_with_run

    def fail(*a, **k):
        raise AssertionError("reframe must not call any model")
    monkeypatch.setattr(remediate, "_edit_image", fail)
    monkeypatch.setattr(remediate, "_compliant_line", fail)
    monkeypatch.setattr(remediate, "_speak", fail)

    change = remediate.apply(run, finding, "reframe", tmp_path / "work", store)

    assert change.method == "reframe"
    assert remediate.localized_master(run, "FR", store).exists()

def test_apply_revoice_renders_a_replacement_line_and_replaces_the_audio(
        store_with_run, tmp_path, fake_models):
    store, run, finding = store_with_run

    change = remediate.apply(run, finding, "revoice", tmp_path / "work", store)

    assert change.method == "revoice"
    assert fake_models["speech"] == ["Solstice. A refreshing drink."]
    assert change.description == 'replaced the spoken line with "Solstice. A refreshing drink."'
    assert remediate.localized_master(run, "FR", store).exists()

def test_apply_revoice_speaks_an_explicit_replacement_line_verbatim(
        store_with_run, tmp_path, fake_models):
    store, run, finding = store_with_run

    remediate.apply(run, finding, "revoice", tmp_path / "work", store,
                    replacement="Solstice, la boisson du soleil.")

    assert fake_models["speech"] == ["Solstice, la boisson du soleil."]
    assert fake_models["lines"] == [], "an explicit line must not be rewritten by a model"

def test_apply_prop_swap_asks_for_a_market_appropriate_replacement(
        store_with_run, tmp_path, fake_models, png_bytes):
    store, run, finding = store_with_run
    fake_models["png"] = png_bytes
    alcohol = _finding(run_id=run.id, id="fnd_FR_FR-ALC-01_obs_shot_1_000",
                       rule_id="FR-ALC-01", severity=95)
    store.add_findings([alcohol])

    change = remediate.apply(run, alcohol, "prop_swap", tmp_path / "work", store)

    assert change.method == "prop_swap"
    instruction = fake_models["edits"][0][0]
    assert "France" in instruction, instruction
    assert "lighting" in instruction and "composition" in instruction

def test_apply_raises_and_leaves_the_master_untouched_when_the_model_returns_no_image(
        store_with_run, tmp_path, monkeypatch):
    store, run, finding = store_with_run

    def no_image(instruction, image_bytes, mime_type="image/png"):
        raise remediate.RemediationError("no image part in the response")
    monkeypatch.setattr(remediate, "_edit_image", no_image)

    with pytest.raises(remediate.RemediationError):
        remediate.apply(run, finding, "relettering", tmp_path / "work", store)

    assert not remediate.localized_master(run, "FR", store).exists()
    assert store.changes(run.id) == []
    # critical: a finding left at "remediating" would be invisible to
    # clearance() and the market would look fixed when nothing was fixed.
    assert store.findings(run.id, "FR")[0].status == "open"
    messages = [m for (_i, _t, _a, m) in store.events_since(run.id, 0)]
    assert any("stage_error: remediate" in m for m in messages), messages


def test_the_picked_intent_becomes_an_instruction_not_the_words_on_screen(
        store_with_run, tmp_path, fake_models, png_bytes):
    """The console's three choices are intents. Passing the label through as
    the replacement is how a re-lettering came back with "Re-letter the text
    in the market's language" painted across the packet."""
    store, run, finding = store_with_run
    fake_models["png"] = png_bytes

    remediate.apply(run, finding, "relettering", tmp_path / "work", store,
                    intent="remove")

    instructions = [message for _i, _t, agent, message
                    in store.events_since(run.id, 0)
                    if "instruction:" in message]
    assert instructions, "the edit instruction was never recorded"
    said = instructions[-1]
    # the directive reaches the model...
    assert "Remove it from the frame entirely" in said
    # ...and the label never becomes words to paint on the frame
    assert "Re-letter the text" not in said
    assert "market's language" not in said


# -- what Veo is told, and what it is not told --

def test_veo_is_told_to_interpolate_not_to_repeat_the_edit():
    """The bridge prompt must not carry the frame-edit instruction.

    It used to. Veo was handed "Replace every alcoholic drink, bottle and
    glass with a non-alcoholic drink... for example tea" -- an instruction
    written for an image editor working on one frame. Veo generates video,
    so it read that as a description of the scene and furnished the set with
    tea: the SA-ROTANA bridge came back with a glass beside a piano player
    who had never held a drink. Both anchors were already correct before Veo
    was called; the edit was finished, and repeating it invented props.
    """
    from customs import remediate
    prompt = remediate._BRIDGE_PROMPT
    # none of the frame-edit vocabulary survives into the video prompt
    assert "Replace every" not in prompt
    assert "Edit this frame" not in prompt
    assert "for example tea" not in prompt
    # it names what it actually is
    assert "motion between these two frames" in prompt


def test_the_bridge_prompt_forbids_inventing_without_forbidding_changing():
    """Provenance, not prohibition.

    "Change nothing" would be the wrong rule and was the first thing I
    wrote: an object that turns or catches the light still has to render,
    and whatever the anchors altered has to stay altered across the span or
    the drink reverts to wine halfway through. The rule is that everything
    on screen must trace back to the two frames -- how it looks is Veo's
    business, whether it exists at all is not.
    """
    from customs import remediate
    prompt = remediate._BRIDGE_PROMPT

    # changing what is there is explicitly allowed
    assert "You may render what is there" in prompt
    assert "must stay in that form for the whole span" in prompt

    # introducing what is not there is explicitly refused
    assert "must not do is introduce" in prompt
    assert "already be visible in one of these two frames" in prompt
    assert "A person holding nothing keeps holding nothing." in prompt


def test_the_frame_edit_does_not_invite_new_props_either(monkeypatch):
    """The anchors are edited by an image model with the same failure mode."""
    from customs import remediate
    swap = remediate._EDIT_INSTRUCTIONS["prop_swap"]
    assert "Do not add any new object anywhere in the frame" in swap
    assert "do not put a replacement where there was nothing before" in swap
    default = remediate._DEFAULT_REPLACEMENT["prop_swap"]
    assert "already visible" in default and "adding none" in default
