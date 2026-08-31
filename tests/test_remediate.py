"""Remediator tests: the method mapping, the guard re-check, and one edit of
each kind applied to a real (tiny, offline) video.

Every model call is faked at this module's own seams -- _edit_image (the
gemini-3.1-flash-image edit), _compliant_line (the replacement copy) and
_speak (TTS) -- which is the same boundary tests/test_adjudicate.py fakes
generate_json/generate_grounded at. Everything below those seams is real:
real ffmpeg, real store writes, real ChangeRecords.
"""
import pathlib
import subprocess
import types

import pytest

from customs import remediate
from customs.schema import Finding, Observation
from customs.store import Store

# --- fixtures / helpers ---

@pytest.fixture(autouse=True)
def _no_image_pacing(monkeypatch):
    """_edit_image waits 31s between calls to stay inside the project's quota
    of two image generations per minute. Tests do not have a minute; the gate
    itself is tested with a fake clock below."""
    monkeypatch.setattr(remediate, "_IMAGE_MIN_INTERVAL_S", 0)


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
    written for an image editor working on one frame, not a brief for a
    video model.

    Be careful about WHY that is wrong, because the obvious story is not
    established. The green can was NOT a Veo failure: _bridge_span had
    hardcoded the alcohol substitution for a modesty finding, so the Gemini
    image editor put the can into BOTH anchors and Veo reproduced what it
    was given, correctly. The only evidence that a Veo prompt's words
    become pixels is the SA-ROTANA tea glass, and nobody checked those
    anchors for a glass before blaming the prompt -- Veo held the edit
    instruction at the same time, so the two channels were never isolated.

    The prompt is still kept task-free, because an instruction a video
    model must satisfy that its two frames do not already entail is a
    plausible way to get content nobody asked for, and the cost of avoiding
    it is nothing. That is a precaution, not a demonstrated law.
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


def test_the_second_anchor_is_edited_against_the_first(monkeypatch, tmp_path):
    """Both ends of a bridge must hold the same object, not two guesses at it.

    Editing the head and the tail independently asks the image model the
    same question twice, and it rarely answers identically -- a taller
    glass, a darker tea, a different fill. Veo then spends the whole span
    morphing one answer into the other, which reads as exactly the artefact
    it is. The tail is therefore edited with the head's result in hand.
    """
    from customs import remediate
    from customs.schema import Finding

    calls = []
    def spy(instruction, image_bytes, mime_type="image/png", reference=None):
        calls.append({"reference": reference})
        return b"edited-" + bytes(str(len(calls)), "ascii")

    monkeypatch.setattr(remediate, "_edit_image", spy)
    monkeypatch.setattr(remediate.media, "extract_keyframes",
                        lambda *a, **k: [tmp_path / "kf.png"])
    monkeypatch.setattr(remediate.media, "fit_image", lambda a, b, c: pathlib.Path(c))
    monkeypatch.setattr(remediate.media, "splice_clip", lambda *a, **k: None)
    monkeypatch.setattr(remediate, "generate_bridge",
                        lambda **kw: kw["out_path"])
    (tmp_path / "kf.png").write_bytes(b"raw")

    finding = Finding(id="f", run_id="r", observation_id="o", market="SA",
                      rule_id="SA-ALC-01", klass="legal", severity=90,
                      t_start=6.0, t_end=11.0, rationale="", citation_ref="",
                      citation_url="", sourced=True, remediable=True,
                      remediation_blocked=False, blocked_reason="")
    remediate._bridge_span(tmp_path / "base.mp4", finding, None,
                           tmp_path, tmp_path / "out.mp4")  # noqa: F841

    assert len(calls) == 2
    assert calls[0]["reference"] is None, "the head has nothing to match yet"
    assert calls[1]["reference"] == b"edited-1", "the tail must match the head"


def test_a_reference_edit_labels_which_image_to_change(monkeypatch):
    """Two bare images in one request is an invitation to edit the wrong one."""
    from customs import remediate
    sent = {}

    class FakePart:
        inline_data = types.SimpleNamespace(data=b"png-out", mime_type="image/png")

    class FakeModels:
        def generate_content(self, model, contents, config):
            sent["parts"] = contents
            content = types.SimpleNamespace(parts=[FakePart()])
            return types.SimpleNamespace(
                candidates=[types.SimpleNamespace(content=content)])

    monkeypatch.setattr(remediate, "client",
                        lambda: types.SimpleNamespace(models=FakeModels()))

    out = remediate._edit_image("swap the drink", b"target", reference=b"already-done")
    assert out == b"png-out"
    texts = [p for p in sent["parts"] if isinstance(p, str)]
    assert any("REFERENCE" in t and "Do not edit this image" in t for t in texts)
    assert any("THE FRAME TO EDIT" in t for t in texts)
    # the image to edit is the last part, so "this image" is unambiguous
    assert not isinstance(sent["parts"][-1], str)


def test_a_bridge_edits_what_the_finding_is_about_not_always_a_drink():
    """The green can.

    A Saudi modesty finding -- "a sleeveless halter top displaying bare
    shoulders and upper arms" -- was bridged by instructing the image model
    to "replace each alcoholic drink with a non-alcoholic drink that suits
    Saudi Arabia". _bridge_span hardcoded the alcohol substitution whatever
    the finding was about. There was no drink in the shot, so the model
    invented one: a green can. No sleeves were ever asked for, so both
    anchors went to Veo still sleeveless, and Veo did exactly what it was
    given -- interpolate between two frames that each had a can in them.
    """
    from customs import remediate
    from customs.schema import Finding

    def make(market, rule):
        return Finding(id="f", run_id="r", observation_id="o", market=market,
                       rule_id=rule, klass="legal", severity=80, t_start=1.0,
                       t_end=5.0, rationale="", citation_ref="", citation_url="",
                       sourced=True, remediable=True, remediation_blocked=False,
                       blocked_reason="")

    modesty = remediate._frame_instruction(make("SA", "SA-MOD-01"), None, None, "Saudi Arabia")
    assert "clothing" in modesty and "covers more of the body" in modesty
    assert "alcoholic" not in modesty and "drink" not in modesty

    # the dimensions that really are substitutions still substitute
    alcohol = remediate._frame_instruction(make("SA-ROTANA", "SA-ALC-01"), None, None, "Rotana")
    assert "alcoholic drink" in alcohol
    text = remediate._frame_instruction(make("FR", "FR-LANG-01"), None, None, "France")
    assert "on-screen text" in text


def test_the_operators_choice_reaches_a_bridge(monkeypatch, tmp_path):
    """Intent was accepted by apply() and then dropped before the bridge.

    _run_method called _bridge_span without passing it, so whichever remedy
    the operator picked in the console was silently discarded for exactly
    the tier that costs real money.
    """
    from customs import remediate
    from customs.schema import Finding

    seen = {}
    monkeypatch.setattr(remediate, "_edit_image",
                        lambda instruction, image_bytes, mime_type="image/png",
                               reference=None: seen.setdefault("instruction", instruction) and b"x" or b"x")
    monkeypatch.setattr(remediate.media, "extract_keyframes",
                        lambda *a, **k: [tmp_path / "kf.png"])
    monkeypatch.setattr(remediate.media, "fit_image", lambda a, b, c: pathlib.Path(c))
    monkeypatch.setattr(remediate.media, "splice_clip", lambda *a, **k: None)
    monkeypatch.setattr(remediate, "generate_bridge", lambda **kw: kw["out_path"])
    (tmp_path / "kf.png").write_bytes(b"raw")

    finding = Finding(id="f", run_id="r", observation_id="o", market="SA",
                      rule_id="SA-MOD-01", klass="legal", severity=80, t_start=1.0,
                      t_end=5.0, rationale="", citation_ref="", citation_url="",
                      sourced=True, remediable=True, remediation_blocked=False,
                      blocked_reason="",
                      remedies=[{"label": "Add sleeves",
                                 "directive": "Extend the halter top into a long-sleeved blouse."}])

    remediate._bridge_span(tmp_path / "b.mp4", finding, None, tmp_path,
                           tmp_path / "out.mp4", intent="remedy:0")
    assert "long-sleeved blouse" in seen["instruction"]


# -- the safety gate: look before you spend --

def _bridge_fixture(monkeypatch, tmp_path, verdicts):
    """A bridge with the image edit and the checker stubbed. Returns what ran."""
    from customs import remediate
    from customs.schema import Finding
    ran = {"edits": 0, "veo": 0, "charged": 0, "events": []}

    monkeypatch.setattr(remediate, "_edit_image",
                        lambda *a, **k: ran.__setitem__("edits", ran["edits"] + 1) or b"png")
    monkeypatch.setattr(remediate, "_anchor_check",
                        lambda *a, **k: verdicts.pop(0))
    monkeypatch.setattr(remediate.media, "extract_keyframes",
                        lambda *a, **k: [tmp_path / "kf.png"])
    monkeypatch.setattr(remediate.media, "fit_image", lambda a, b, c: pathlib.Path(c))
    monkeypatch.setattr(remediate.media, "splice_clip", lambda *a, **k: None)
    def veo(**kw):
        ran["veo"] += 1
        return kw["out_path"]
    monkeypatch.setattr(remediate, "generate_bridge", veo)
    (tmp_path / "kf.png").write_bytes(b"raw")

    finding = Finding(id="f", run_id="r", observation_id="o", market="SA",
                      rule_id="SA-MOD-01", klass="legal", severity=80,
                      t_start=1.0, t_end=5.0, rationale="", citation_ref="",
                      citation_url="", sourced=True, remediable=True,
                      remediation_blocked=False, blocked_reason="")
    return remediate, finding, ran


def test_an_unfixed_anchor_never_reaches_veo_and_is_never_charged(monkeypatch, tmp_path):
    """The whole point: a wasted bridge costs EUR 1.88, a check costs cents.

    A modesty finding once came back as a green drinking can -- four
    generated seconds, fully charged, for a frame whose sleeves were never
    touched. Nothing looked at the anchor before Veo was paid to move it.
    """
    bad = {"fixed": False, "still_visible": "bare shoulders", "added": "a green can"}
    remediate, finding, ran = _bridge_fixture(
        monkeypatch, tmp_path, [dict(bad), dict(bad)])   # fails, retries, fails

    with pytest.raises(remediate.RemediationError, match="still does not satisfy"):
        remediate._bridge_span(tmp_path / "b.mp4", finding, None, tmp_path,
                               tmp_path / "out.mp4",
                               spend=lambda: ran.__setitem__("charged", ran["charged"] + 1),
                               on_event=lambda a, m: ran["events"].append(m))

    assert ran["edits"] == 2, "one retry, told what was wrong"
    assert ran["veo"] == 0, "Veo must not be called"
    assert ran["charged"] == 0, "and nothing charged"
    assert any("rejected" in m for m in ran["events"])


def test_a_retry_that_works_goes_through_and_pays(monkeypatch, tmp_path):
    """The first edit missed, the second landed. That is worth generating."""
    remediate, finding, ran = _bridge_fixture(monkeypatch, tmp_path, [
        {"fixed": False, "still_visible": "bare shoulders", "added": ""},
        {"fixed": True, "still_visible": "", "added": ""},   # head, second try
        {"fixed": True, "still_visible": "", "added": ""},   # tail, first try
    ])
    remediate._bridge_span(tmp_path / "b.mp4", finding, None, tmp_path,
                           tmp_path / "out.mp4",
                           spend=lambda: ran.__setitem__("charged", ran["charged"] + 1),
                           on_event=lambda a, m: ran["events"].append(m))
    assert ran["edits"] == 3 and ran["veo"] == 1
    assert ran["charged"] == 1, "charged once, at the point Veo was called"


def test_an_edit_that_smuggles_in_a_new_object_is_rejected(monkeypatch, tmp_path):
    """Fixed but with something added is not fixed. That is the green can."""
    remediate, finding, ran = _bridge_fixture(monkeypatch, tmp_path, [
        {"fixed": True, "still_visible": "", "added": "a green drinking can"},
        {"fixed": True, "still_visible": "", "added": "a green drinking can"},
    ])
    with pytest.raises(remediate.RemediationError, match="green drinking can"):
        remediate._bridge_span(tmp_path / "b.mp4", finding, None, tmp_path,
                               tmp_path / "out.mp4", spend=lambda: None)
    assert ran["veo"] == 0


def test_a_broken_checker_does_not_block_a_fix(monkeypatch, tmp_path):
    """A checker that errors must not become a new way for remediation to fail."""
    remediate, finding, ran = _bridge_fixture(
        monkeypatch, tmp_path, [{"unchecked": True}, {"unchecked": True}])
    remediate._bridge_span(tmp_path / "b.mp4", finding, None, tmp_path,
                           tmp_path / "out.mp4",
                           spend=lambda: ran.__setitem__("charged", 1),
                           on_event=lambda a, m: ran["events"].append(m))
    assert ran["veo"] == 1 and ran["charged"] == 1
    assert any("could not be checked" in m for m in ran["events"])


def test_nothing_from_the_run_can_leak_into_the_veo_prompt(monkeypatch, tmp_path):
    """The real guardrail: assert the value AT THE CALL SITE, not the constant.

    The other prompt tests assert on remediate._BRIDGE_PROMPT. That checks
    the constant is well written, not that it is what gets sent -- today
    `prompt=_BRIDGE_PROMPT + statement` would ship with those tests green.

    Veo must receive one of a finite set of strings. Nothing derived from
    the run may reach it: the observation describes the PRE-EDIT pixels, so
    a finding about wine would argue for wine against anchors that now show
    tea.
    """
    from customs import remediate
    from customs.schema import Finding

    sent = {}
    monkeypatch.setattr(remediate, "_edit_image", lambda *a, **k: b"png")
    monkeypatch.setattr(remediate, "_anchor_check",
                        lambda *a, **k: {"fixed": True, "still_visible": "", "added": ""})
    monkeypatch.setattr(remediate.media, "extract_keyframes",
                        lambda *a, **k: [tmp_path / "kf.png"])
    monkeypatch.setattr(remediate.media, "fit_image", lambda a, b, c: pathlib.Path(c))
    monkeypatch.setattr(remediate.media, "splice_clip", lambda *a, **k: None)
    def veo(**kw):
        sent["prompt"] = kw["prompt"]
        return kw["out_path"]
    monkeypatch.setattr(remediate, "generate_bridge", veo)
    (tmp_path / "kf.png").write_bytes(b"raw")

    secret = "a woman in a sleeveless halter top holding a glass of red wine"
    finding = Finding(id="f", run_id="r", observation_id="o", market="SA",
                      rule_id="SA-MOD-01", klass="legal", severity=80,
                      t_start=1.0, t_end=5.0,
                      rationale=secret, citation_ref=secret, citation_url="",
                      sourced=True, remediable=True, remediation_blocked=False,
                      blocked_reason="",
                      remedies=[{"label": "x", "directive": secret}])

    remediate._bridge_span(tmp_path / "b.mp4", finding, secret, tmp_path,
                           tmp_path / "out.mp4", intent="remedy:0",
                           statement=secret, spend=lambda: None)

    assert sent["prompt"] in {remediate._BRIDGE_PROMPT}, \
        "Veo's prompt must be one of a finite set, not built per finding"
    for leaked in ("wine", "halter", "sleeveless", "woman", "SA-MOD-01"):
        assert leaked not in sent["prompt"], f"{leaked!r} reached Veo"


def test_a_blocked_generation_is_never_charged(monkeypatch, tmp_path):
    """Google says it plainly: "You will not be charged for blocked videos."

    A SA-ROTANA alcohol bridge came back rai_media_filtered_count=1 and the
    operator's budget was debited EUR 1.88 for a video that was never
    produced and never billed. The charge was written down BEFORE the call.
    """
    from customs import remediate
    from customs.genai_client import VeoBlocked
    from customs.schema import Finding

    charged, tries = [], []
    monkeypatch.setattr(remediate, "_edit_image", lambda *a, **k: b"png")
    monkeypatch.setattr(remediate, "_anchor_check",
                        lambda *a, **k: {"fixed": True, "still_visible": "", "added": ""})
    monkeypatch.setattr(remediate.media, "extract_keyframes",
                        lambda *a, **k: [tmp_path / "kf.png"])
    monkeypatch.setattr(remediate.media, "fit_image", lambda a, b, c: pathlib.Path(c))
    monkeypatch.setattr(remediate.media, "splice_clip", lambda *a, **k: None)
    def blocked(**kw):
        tries.append(1)
        raise VeoBlocked("filtered")
    monkeypatch.setattr(remediate, "generate_bridge", blocked)
    (tmp_path / "kf.png").write_bytes(b"raw")

    finding = Finding(id="f", run_id="r", observation_id="o", market="SA-ROTANA",
                      rule_id="SA-ALC-01", klass="legal", severity=90, t_start=1.0,
                      t_end=5.0, rationale="", citation_ref="", citation_url="",
                      sourced=True, remediable=True, remediation_blocked=False,
                      blocked_reason="")

    with pytest.raises(remediate.RemediationError, match="safety filter"):
        remediate._bridge_span(tmp_path / "b.mp4", finding, None, tmp_path,
                               tmp_path / "out.mp4", spend=lambda: charged.append(1))

    assert len(tries) == 2, "the filter is stochastic; a free retry is worth taking"
    assert charged == [], "a blocked generation must not touch the budget"


def test_a_generation_that_fails_any_other_way_is_charged(monkeypatch, tmp_path):
    """Veo did the work and billed for it, whether or not it came back."""
    from customs import remediate
    from customs.schema import Finding

    charged = []
    monkeypatch.setattr(remediate, "_edit_image", lambda *a, **k: b"png")
    monkeypatch.setattr(remediate, "_anchor_check",
                        lambda *a, **k: {"fixed": True, "still_visible": "", "added": ""})
    monkeypatch.setattr(remediate.media, "extract_keyframes",
                        lambda *a, **k: [tmp_path / "kf.png"])
    monkeypatch.setattr(remediate.media, "fit_image", lambda a, b, c: pathlib.Path(c))
    monkeypatch.setattr(remediate.media, "splice_clip", lambda *a, **k: None)
    def timeout(**kw):
        raise RuntimeError("Veo bridge still running after 600s")
    monkeypatch.setattr(remediate, "generate_bridge", timeout)
    (tmp_path / "kf.png").write_bytes(b"raw")

    finding = Finding(id="f", run_id="r", observation_id="o", market="SA",
                      rule_id="SA-ALC-01", klass="legal", severity=90, t_start=1.0,
                      t_end=5.0, rationale="", citation_ref="", citation_url="",
                      sourced=True, remediable=True, remediation_blocked=False,
                      blocked_reason="")
    with pytest.raises(RuntimeError, match="still running"):
        remediate._bridge_span(tmp_path / "b.mp4", finding, None, tmp_path,
                               tmp_path / "out.mp4", spend=lambda: charged.append(1))
    assert charged == [1], "a real generation that died is still billed by Google"


def test_one_shot_is_one_edit_even_when_it_carries_several_findings():
    """The operator's complaint, as a unit test.

    "1 scene causes 3 screenshots and 3 problems, and when 1 screenshot
    problem is fixed, the full scene start to stop is not fixed."

    Measured on a real run: SA span 35.083-42.083 carries three open
    findings on one seven-second shot. Fixing them one at a time
    regenerated those same seven seconds three times at EUR 3.68 each,
    and every pass after the first took its anchors from the previous
    pass's OUTPUT -- Veo generating from Veo.
    """
    from customs import remediate
    from customs.schema import Finding

    def _f(fid, rule, status="open", shot="shot_7", t=(35.083, 42.083), market="SA"):
        return Finding(
            id=fid, run_id="run_1", observation_id=f"obs_{fid}", market=market,
            rule_id=rule, klass="legal", severity=90, t_start=t[0], t_end=t[1],
            rationale=f"something about {rule}", citation_ref="x", citation_url="",
            sourced=True, remediable=True, remediation_blocked=False,
            blocked_reason="", status=status, shot_id=shot)

    target = _f("f1", "SA-ALC-01")
    all_findings = [
        target,
        _f("f2", "SA-MOD-01"),
        _f("f3", "SA-PHARMA-01"),
        _f("f4", "SA-ALC-01", status="resolved"),          # already dealt with
        _f("f5", "SA-TOB-01", shot="shot_8", t=(50.0, 55.0)),  # a different shot
        _f("f6", "FR-ALC-01", market="FR"),                # a different market
    ]

    company = remediate.siblings_in_shot(target, all_findings)
    assert {f.id for f in company} == {"f2", "f3"}, \
        "only the open findings on this shot, in this market"

    # and the instruction they produce asks for all of it at once
    combined = remediate._combined_directive(
        target, company, replacement=None, intent=None, market_name="Saudi Arabia")
    assert "SA-MOD-01" in combined or "something about SA-MOD-01" in combined
    assert "SA-PHARMA-01" in combined or "something about SA-PHARMA-01" in combined
    assert "change nothing else" in combined


def test_a_finding_with_no_shot_id_still_groups_by_its_span():
    """shot_id is new. Findings judged before it exists still share a span,
    which is what a shared shot looked like then."""
    from customs import remediate
    from customs.schema import Finding

    def _f(fid, rule, t):
        return Finding(
            id=fid, run_id="run_1", observation_id=f"obs_{fid}", market="SA",
            rule_id=rule, klass="legal", severity=90, t_start=t[0], t_end=t[1],
            rationale="x", citation_ref="x", citation_url="", sourced=True,
            remediable=True, remediation_blocked=False, blocked_reason="",
            status="open")           # no shot_id at all

    target = _f("f1", "SA-ALC-01", (35.083, 42.083))
    same = _f("f2", "SA-MOD-01", (35.083, 42.083))
    other = _f("f3", "SA-TOB-01", (12.0, 19.0))

    company = remediate.siblings_in_shot(target, [target, same, other])
    assert {f.id for f in company} == {"f2"}


def test_a_guard_blocked_finding_is_never_swept_into_someone_elses_edit():
    """The guard takes auto-remediation off the table for a reason. A
    finding it blocked must not be quietly fixed as a passenger on a fix
    for something else."""
    from customs import remediate
    from customs.schema import Finding

    def _f(fid, blocked):
        return Finding(
            id=fid, run_id="run_1", observation_id=f"obs_{fid}", market="SA",
            rule_id="SA-LGBT-01" if blocked else "SA-ALC-01", klass="legal",
            severity=90, t_start=1.0, t_end=5.0, rationale="x", citation_ref="x",
            citation_url="", sourced=True, remediable=not blocked,
            remediation_blocked=blocked,
            blocked_reason="protected basis" if blocked else "",
            status="open", shot_id="shot_1")

    target = _f("f1", False)
    company = remediate.siblings_in_shot(target, [target, _f("f2", True)])
    assert company == [], "the guard's decision is not a passenger seat"


def test_a_quota_rejection_is_slept_off_once_not_fatal_to_the_bridge(monkeypatch):
    """The 429 that killed two bridges landed on the second image call of the
    same bridge -- the tail edit, and an anchor retry -- with a correct head
    already in hand. One sleep past the minute boundary saves the bridge."""
    from customs import remediate

    class Quota(Exception):
        code = 429

    class FakePart:
        inline_data = types.SimpleNamespace(data=b"png-out", mime_type="image/png")

    calls, slept = [], []

    class FakeModels:
        def generate_content(self, model, contents, config):
            calls.append(model)
            if len(calls) == 1:
                raise Quota("429 RESOURCE_EXHAUSTED")
            content = types.SimpleNamespace(parts=[FakePart()])
            return types.SimpleNamespace(
                candidates=[types.SimpleNamespace(content=content)])

    monkeypatch.setattr(remediate, "client",
                        lambda: types.SimpleNamespace(models=FakeModels()))
    monkeypatch.setattr(remediate.time, "sleep", slept.append)

    assert remediate._edit_image("cover the shoulders", b"frame") == b"png-out"
    assert len(calls) == 2, "the quota rejection must be retried, once"
    assert slept == [remediate._QUOTA_BACKOFF]


def test_a_second_quota_rejection_is_raised_rather_than_slept_on_forever(monkeypatch):
    from customs import remediate

    class Quota(Exception):
        code = 429

    class FakeModels:
        def generate_content(self, model, contents, config):
            raise Quota("429 RESOURCE_EXHAUSTED")

    monkeypatch.setattr(remediate, "client",
                        lambda: types.SimpleNamespace(models=FakeModels()))
    monkeypatch.setattr(remediate.time, "sleep", lambda _s: None)

    with pytest.raises(Quota):
        remediate._edit_image("cover the shoulders", b"frame")


def test_no_image_back_carries_the_refusal_reason_not_just_the_absence(monkeypatch):
    """"returned no image part" alone reads as a fault. It is nearly always a
    refusal -- a modesty edit asks the model to re-dress a real person -- and
    the reason was being thrown away with the text part."""
    from customs import remediate

    class FakeModels:
        def generate_content(self, model, contents, config):
            content = types.SimpleNamespace(
                parts=[types.SimpleNamespace(inline_data=None,
                                             text="I can't edit clothing on people.")])
            return types.SimpleNamespace(
                candidates=[types.SimpleNamespace(content=content,
                                                 finish_reason="IMAGE_SAFETY")],
                prompt_feedback=types.SimpleNamespace(block_reason="PROHIBITED_CONTENT"))

    monkeypatch.setattr(remediate, "client",
                        lambda: types.SimpleNamespace(models=FakeModels()))

    with pytest.raises(remediate.RemediationError) as caught:
        remediate._edit_image("cover the shoulders", b"frame")
    message = str(caught.value)
    assert "IMAGE_SAFETY" in message
    assert "PROHIBITED_CONTENT" in message
    assert "edit clothing on people" in message


def test_the_image_pacer_lets_the_first_call_through_and_spaces_the_next(monkeypatch):
    """Two image generations per minute is the project's whole budget, and a
    bridge wants two. Asking for them back to back is what made the 429
    arithmetic rather than bad luck."""
    now, slept = [1000.0], []
    monkeypatch.setattr(remediate, "_IMAGE_MIN_INTERVAL_S", 31)
    monkeypatch.setattr(remediate, "_last_image_call", 0.0)
    monkeypatch.setattr(remediate.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(remediate.time, "sleep",
                        lambda s: (slept.append(s), now.__setitem__(0, now[0] + s)))

    remediate._pace_image_call()
    assert slept == [], "the first call has nothing to wait for"

    now[0] += 5
    remediate._pace_image_call()
    assert slept == [26.0], "five seconds in, 26 are still owed"

    now[0] += 40
    remediate._pace_image_call()
    assert slept == [26.0], "a call that is already late waits for nothing"
