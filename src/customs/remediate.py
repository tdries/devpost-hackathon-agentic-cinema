"""The Remediator: turn one finding into one edit of the localized master.

Design spec section 10. The methods, ordered by cost and by risk of looking
fake, proven on real pixels and real audio:

    relettering  the on-screen text is re-lettered in the market's language
    prop_swap    the offending prop becomes a market-appropriate one
    revoice      the offending line is re-spoken, compliant, over the same span

There used to be a fourth cheap method, reframe: a centre crop that punched
in on the whole frame for the span. It was removed 2026-08-31 because it was
useless in practice -- it zoomed the shot without knowing where the offending
element was, so it degraded every pixel to maybe exclude one. prop_swap is
the fallback now: the finding carries a box, the edit lands through it.

Every method produces exactly one artifact -- runs/{run_id}/localized_{market}.mp4
-- and every method appends to it rather than forking a new file: the first
edit for a market starts from the original asset, every later one starts from
the master the previous edit wrote. One asset per market, however many
findings it took.

Nothing is edited silently. Every apply() writes a before and an after still
to runs/{run_id}/changes/, persists a ChangeRecord naming the finding, the
method and both stills, and emits mission events as it goes.

--- Image editing on this project ---

The spec says "Imagen inpainting". Task 2 probed Vertex live and found no
Imagen model reachable from this project in any region (five ids, five
regions, all 404), so image editing here is Gemini-native: the keyframe goes
in as an image Part alongside an edit instruction, and the edited frame comes
back as an inline_data part of the response. Same job, different endpoint,
one seam (_edit_image) so a future Imagen path is a one-function change.

--- Why the guard is re-checked here ---

guard.apply already decided, at judging time, that a protected-basis finding
may not be auto-remediated. This module refuses those findings again, on its
own, before it touches a frame (RemediationBlocked). Defense in depth: the
webhook resolves a finding from alert labels, and an alert is an external
input. The single most damaging thing this system could do is edit away a
protected characteristic because a forged or stale alert asked it to, so
"never remediate that" is enforced at the point of action too, not only at
the point of judgement.
"""
import logging
import re
import threading
import time
import uuid
import wave
from pathlib import Path

from google.genai import types

from customs import costs, media
from customs.config import settings
from customs.genai_client import (OmniQuota, VeoBlocked, VeoRefusedInput,
                                  client, generate_bridge, generate_json,
                                  generate_json_image, generate_omni_edit)
from customs.media import Shot
from customs.packs import load as load_packs
from customs.schema import ChangeRecord, Finding, Observation

log = logging.getLogger(__name__)

class RemediationBlocked(Exception):
    """Raised when a finding must never be auto-remediated (the guard's call)."""

class RemediationError(Exception):
    """Raised when an edit could not be produced (no image back, no audio back)."""

# The image model's ceiling, read from the project's own Vertex quotas
# rather than guessed: gemini-3.1-flash-image appears in none of the 366
# quotaInfos for this project, so it takes the default of
#
#   GenContentImageGenRequestsPerMinutePerProjectPerBaseModelGlobal = 2
#
# TWO image-generation requests per minute, for the whole project. That is
# the entire budget a bridge has, and a bridge spends exactly two on a clean
# run (head anchor, tail anchor) -- so an unspaced pair uses the minute up
# and any retry is a certain 429, which is what happened four times on the
# Chanel spot. Reacting to the 429 cannot win; the calls have to be spaced
# so the second one is asking a fresh minute.
#
# ponytail: one process-wide gate, because the quota is project-wide and
# this service runs a single instance by design (see scripts/deploy.sh). A
# second replica would need a shared one, and would also halve everyone's
# budget, so this is the same assumption the rest of the service already
# makes rather than a new one.
#
# The real fix is a quota increase on that metric. Until then this trades
# roughly 30 seconds per bridge for a bridge that completes, which against
# a Veo generation of several minutes and EUR 1.88 is not a close call.
_IMAGE_MIN_INTERVAL_S = 31
_image_gate = threading.Lock()
_last_image_call = 0.0

# Longer than a minute: a 429 means the window is already spent, and the
# next one does not open until 60s after the call that spent it.
_QUOTA_BACKOFF = 65


def _pace_image_call() -> None:
    """Block until this process may make another image-generation request.

    The lock is held across the sleep on purpose: the quota is counted per
    project, not per caller, so two threads waiting independently would
    both wake and both spend the same minute.
    """
    global _last_image_call
    with _image_gate:
        waited = time.monotonic() - _last_image_call
        if waited < _IMAGE_MIN_INTERVAL_S:
            delay = _IMAGE_MIN_INTERVAL_S - waited
            log.info("pacing the image model: %.0fs, to stay inside "
                     "2 requests per minute", delay)
            time.sleep(delay)
        _last_image_call = time.monotonic()

# "bridge" is never chosen by plan(): it regenerates pixels and costs real
# money, so it only ever runs because an operator picked it in the console
# and the day's budget allowed it.
METHODS = ("relettering", "prop_swap", "revoice", "per_frame", "omni", "bridge")

# --- the mapping table (task-14 contract) ---
#
# dimension                      method       why
# -----------------------------  -----------  ---------------------------------
# text_legibility                relettering  the violation IS the on-screen text
# alcohol_tobacco_drugs          prop_swap    a physical prop on set: bottle, glass, pack
# food_and_animals               prop_swap    also a prop: the dish, the animal on the table
# health_claims_pharma           revoice      a claim, which in an ad is spoken
# comparative_claims             revoice      same, unless the observation says it is
#                                             on-screen text, and then relettering
# everything else                prop_swap    the generic image edit: the
#                                             instruction comes from the intent
#                                             or the judge's remedy, and the
#                                             edit lands through the finding's
#                                             own box
#
# The dimension is the observation's own (the analyst assigned it, and
# adjudicate.candidates only ever pairs an observation with a rule of the same
# dimension, so the finding's dimension IS the observation's). Without the
# observation in hand, it is read back off the market pack by rule_id.
METHOD_BY_DIMENSION = {
    "text_legibility": "relettering",
    "alcohol_tobacco_drugs": "prop_swap",
    "food_and_animals": "prop_swap",
    "health_claims_pharma": "revoice",
    "comparative_claims": "revoice",
}
DEFAULT_METHOD = "prop_swap"

# Claims can be made either way round. When the observation quotes on-screen
# text rather than speech, a claims finding is re-lettered, not re-voiced.
_CLAIM_DIMENSIONS = ("health_claims_pharma", "comparative_claims")
_ON_SCREEN_MARKERS = (
    "on-screen", "on screen", "text reads", "reads ", "caption", "subtitle",
    "superimposed", "written", "printed", "label", "sign",
)

# What the operator picked in the console is an INTENT, not the words to
# put on screen. Sending the label through as `replacement` is how a
# re-lettering came back with "Re-letter the text in the market's language"
# painted across the packet: the label is a description of the job, and the
# job is to work out the words. An intent picks the instruction; only a
# human typing exact text sets `replacement`.
def _directive_for(finding, intent: str | None) -> str | None:
    """The edit instruction an intent stands for.

    "remedy:N" indexes the judge's own remedies on the finding, which is why
    the directive never travels through the browser: the form submits a
    position, not model-written prose that could end up painted onto the
    frame the way an intent label once was.
    """
    if not intent:
        return None
    if intent.startswith("remedy:"):
        try:
            return (getattr(finding, "remedies", None) or [])[int(intent[7:])]["directive"]
        except (ValueError, IndexError, KeyError, TypeError):
            return None
    return INTENT_INSTRUCTIONS.get(intent)

INTENT_INSTRUCTIONS = {
    "translate": None,          # the default: the model translates for the market
    "swap": None,               # the default: a market-appropriate substitute
    "adult": "Replace the child with an adult doing exactly the same thing.",
    "object": "Replace the person with an inanimate object of similar size.",
    "empty": "Keep the container exactly as it is and change only its contents "
             "to a non-alcoholic drink of a different colour.",
    "remove": "Remove it from the frame entirely and reconstruct what would be "
              "behind it from the surrounding pixels.",
    "neutral": "Replace it with a plain, neutral element that matches the "
               "surroundings and carries no words, symbols or branding.",
    "cover": "Extend the existing clothing so that it covers more of the body, "
             "in the same fabric, colour and lighting.",
    "replace": "Replace the garment with a modest one in the same colour and "
               "fabric, leaving the person and the pose unchanged.",
    "qualify": "Rewrite the on-screen claim so it is qualified and "
               "substantiated, in the same typeface and position.",
    "soften": "Rewrite the on-screen claim without any comparison, in the same "
              "typeface and position.",
    "drop": "Remove the claim from the frame, reconstructing the surface "
            "behind it.",
    "slow": None,
    "dim": None,
    "cut": None,
    "disclaim": None,
}

_EDIT_INSTRUCTIONS = {
    "relettering": (
        "Edit this frame from a television commercial. Replace the on-screen "
        "text with {replacement}, in the same handwriting or typeface, the "
        "same colour, the same size and the same position. Keep the paper, "
        "the lighting, the focus and every other pixel of the frame identical. "
        "Change nothing except the words themselves."
    ),
    "prop_swap": (
        "Edit this frame from a television commercial. {replacement} Keep the "
        "lighting, the composition, the camera angle, the hands and the "
        "people exactly as they are, and change nothing else in the frame. "
        "Do not add any new object anywhere in the frame, and do not put a "
        "replacement where there was nothing before: only what is already "
        "there may change."
    ),
}
# What Veo is asked to do, and it is NOT the edit.
#
# This used to hand Veo the frame-edit instruction verbatim -- "Replace every
# alcoholic drink, bottle and glass with a non-alcoholic drink... for example
# tea". Veo is not editing a frame, it is generating video, so it read that
# as a description of the scene to render and furnished the whole set with
# tea: the SA-ROTANA bridge came back with a glass beside the piano player,
# who had never had a drink of any kind.
#
# The line this prompt has to draw is not "change nothing". Veo must be free
# to change what is already there -- an object that turns, catches the light
# or passes behind a hand still has to render, and whatever the anchors
# altered has to stay altered for the whole span or the drink reverts to
# wine halfway through. What it must not do is INVENT: put something into
# the scene that neither anchor shows.
#
# So the test is provenance, not permission. Everything in the finished span
# has to trace back to something visible in the two frames. How it looks is
# Veo's business; whether it exists at all is not.
_BRIDGE_PROMPT = (
    "Generate only the motion between these two frames of a television "
    "commercial. Both frames are already correct as they are: reproduce the "
    "scene they show and carry it continuously from the first frame to the "
    "last, moving only as the camera and the performers move.\n\n"
    "Everything visible in the finished shot must already be visible in one "
    "of these two frames. You may render what is there as it turns, moves, "
    "or changes with the light, and anything the two frames show in a "
    "particular form must stay in that form for the whole span. What you "
    "must not do is introduce anything: no extra object, prop, drink, "
    "container, food, garment, text or person anywhere in the scene, nothing "
    "put into anyone's hands, and nothing set down on a table, a surface or "
    "into the background that these two frames do not already show. A person "
    "holding nothing keeps holding nothing.\n\n"
    "Same camera move, same lighting, same performers, same wardrobe, same "
    "set, same framing."
)

# Appended to the anchor edit when Veo's input filter refuses the frames.
# Same goal, different picture: Veo's gate is far more conservative about
# people and skin than the image editor that produced the frame or the
# check that passed it, so the second attempt over-corrects on purpose.
_ANCHOR_RETRY_EXTRA = (
    " A downstream safety filter refused the previous version of this frame. "
    "Redo the edit more conservatively toward the same goal: where a person "
    "is visible, make the clothing unmistakably modest -- opaque fabric, "
    "covered shoulders, chest and midriff -- and mute anything else in the "
    "frame a strict reviewer could object to. Change as little of the rest "
    "of the frame as possible.")

_DEFAULT_REPLACEMENT = {
    "relettering": "the same sentence translated into the language of {market_name}",
    "prop_swap": (
        # One dimension covers alcohol, tobacco AND drugs, so this default
        # cannot name only alcohol: a Parliament cigarette pack was being
        # sent "replace each alcoholic drink, bottle and glass", the same
        # category collapse that put a green can into a modesty shot.
        "Replace each alcoholic drink, bottle, glass, cigarette, cigar, "
        "tobacco pack or ashtray that is already visible with a permitted "
        "equivalent for {market_name} -- tea in the same style of glass in "
        "place of a drink, a pen or nothing at all in place of a cigarette, "
        "a closed box with no branding in place of a pack -- keeping the "
        "same number of items in the same places and adding none."
    ),
}

_LINE_PROMPT = (
    "You are rewriting one line of advertising voice-over so it complies with "
    "the advertising rules of {market_name}. The line currently breaks this "
    "rule: {rule_basis}. Why it breaks it: {rationale}. Write one replacement "
    "line for the same product (a soft drink called Solstice), no longer than "
    "the original, that says something appealing without making the "
    "non-compliant claim. Return only the line itself."
)
_LINE_SCHEMA = {
    "type": "object",
    "properties": {"line": {"type": "string"}},
    "required": ["line"],
}

# The voice the test asset's own voice-over was rendered with
# (scripts/make_test_ad.py), so a revoiced line sounds like the same speaker.
TTS_VOICE = "Charon"
_TTS_DEFAULT_RATE = 24000
_PCM_RATE_RE = re.compile(r"rate=(\d+)")

_packs_cache: dict | None = None

def _packs() -> dict:
    global _packs_cache
    if _packs_cache is None:
        _packs_cache = load_packs()
    return _packs_cache

def _rule_for(finding: Finding):
    pack = _packs().get(finding.market)
    if pack is None:
        return None
    return next((r for r in pack.rules if r.id == finding.rule_id), None)

def _market_name(market: str) -> str:
    pack = _packs().get(market)
    return pack.name if pack else market

def _dimension_of(finding: Finding, observation: Observation | None) -> str:
    if observation is not None:
        return observation.dimension
    rule = _rule_for(finding)
    return rule.dimension if rule else ""

def plan(finding: Finding, observation: Observation | None = None) -> str:
    """Choose the remediation method for one finding. See METHOD_BY_DIMENSION.

    Pure: no model call, no I/O beyond the (cached) market pack read. Passing
    the finding's own Observation is what lets a claims finding made in
    on-screen text be re-lettered instead of re-voiced; without it the
    dimension's default applies.
    """
    dimension = _dimension_of(finding, observation)
    method = METHOD_BY_DIMENSION.get(dimension, DEFAULT_METHOD)
    if method == "revoice" and dimension in _CLAIM_DIMENSIONS and observation is not None:
        statement = observation.statement.lower()
        if any(marker in statement for marker in _ON_SCREEN_MARKERS):
            return "relettering"
    return method

# --- one writer per localized master ---
#
# apply() is a read-modify-write of runs/{run_id}/localized_{market}.mp4: it
# reads the current master, edits it, and replaces it. Two alerts for the same
# market arrive as two requests, and app.remediate_and_verify is a sync
# function, which Starlette runs through run_in_threadpool -- so two of them
# really do run in two OS threads at once. Without this lock the second edit
# starts from the master the first one read, finishes last, and silently
# reverts a verified fix.
#
# Ceiling, stated plainly: this is a per-process lock. It is correct for one
# Cloud Run instance and buys nothing across two. The fix at that point is a
# queue in front of remediation or a lease column in the run store, not a
# bigger lock -- and this is a demo-grade system where one instance is the
# deployment. Keyed per (run_id, market) rather than globally so two markets
# of one run, or two runs, still remediate in parallel: they write different
# files.
#
# RLock, not Lock: apply() takes it for its own sake, and a caller that holds
# it across apply + verify.confirm (app.remediate_and_verify does, because the
# verifier reads the same master) must not deadlock against itself.
_market_locks: dict[tuple[str, str], threading.RLock] = {}
_market_locks_guard = threading.Lock()

def market_lock(run_id: str, market: str) -> threading.RLock:
    """The lock guarding one run's localized master for one market.

    Hold it across every read-modify-write of that file. apply() takes it
    itself; a caller that also needs the master to stay still afterwards (the
    verifier re-observes it) should hold it around both.
    """
    key = (run_id, market)
    with _market_locks_guard:
        lock = _market_locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _market_locks[key] = lock
        return lock

def run_dir(run, store) -> Path:
    """runs/{run_id}/ -- this run's artifact directory.

    Derived from the store's own file rather than a hardcoded "runs/" so a
    test store in a tmp directory keeps its artifacts next to it. In
    production settings.db_path is runs/customs.db, which makes this exactly
    the runs/{run_id}/ the design spec names.
    """
    return Path(store.db_path).parent / run.id

def localized_master(run, market: str, store) -> Path:
    """runs/{run_id}/localized_{market}.mp4 -- one edited master per market."""
    return run_dir(run, store) / f"localized_{market}.mp4"

def _refuse_if_blocked(finding: Finding) -> None:
    if finding.remediation_blocked:
        raise RemediationBlocked(
            f"{finding.id} is blocked from auto-remediation: "
            f"{finding.blocked_reason or 'no reason recorded'}"
        )
    if finding.klass == "offence":
        raise RemediationBlocked(f"{finding.id} is an offence finding; never auto-edited")
    if not finding.remediable:
        raise RemediationBlocked(f"{finding.id} is not remediable")

# --- model seams (faked wholesale in tests) ---

def _edit_image(instruction: str, image_bytes: bytes, mime_type: str = "image/png",
                reference: bytes | None = None) -> bytes:
    """One image edit: the frame in, the edited frame out.

    Gemini-native editing (see the module docstring on Imagen): the frame is
    an input Part next to the instruction, and the response carries the
    edited frame as an inline_data part. A text part alongside the pixels is
    ignored; a text part INSTEAD of pixels is all the explanation there is
    for a failed edit, so it is reported rather than dropped.

    `reference` is a frame from the same shot that has already been edited.
    A bridge edits two anchors, and editing them independently gives two
    different answers to the same question -- a taller glass, a darker tea,
    a different fill -- which Veo then has to morph between across the span.
    Handing the second edit the first one's result is what keeps them the
    same object. The parts are labelled because two bare images in one
    request is an invitation to edit the wrong one.
    """
    parts: list = [instruction]
    if reference is not None:
        parts += [
            "REFERENCE. This frame is from the same shot and has already been "
            "corrected. Whatever replacement it shows, reproduce that exact "
            "object: same kind, same colour, same material, same fill level, "
            "same proportions. Do not edit this image.",
            types.Part.from_bytes(data=reference, mime_type=mime_type),
            "THE FRAME TO EDIT. Apply the instruction to this image only, and "
            "return this image edited.",
        ]
    parts += [types.Part.from_bytes(data=image_bytes, mime_type=mime_type)]
    # Paced, not merely retried. At two requests per minute a bridge's two
    # anchors are the whole budget, so asking for them back to back spends
    # the minute and the 429 is arithmetic rather than bad luck. The retry
    # below is what is left for when the pacing is not enough -- another
    # run in the same project, or a window this process cannot see.
    for attempt in (1, 2):
        try:
            _pace_image_call()
            response = client().models.generate_content(
                model=settings.model_image,
                contents=parts,
                config=types.GenerateContentConfig(
                    response_modalities=["TEXT", "IMAGE"]),
            )
            break
        except Exception as exc:  # noqa: BLE001 -- only quota is retried
            quota = (getattr(exc, "code", None) == 429
                     or "RESOURCE_EXHAUSTED" in str(exc))
            if not quota or attempt == 2:
                raise
            log.warning("%s hit its quota, retrying the edit in %ss",
                        settings.model_image, _QUOTA_BACKOFF)
            time.sleep(_QUOTA_BACKOFF)
    for candidate in (response.candidates or []):
        for part in (getattr(candidate.content, "parts", None) or []):
            inline = getattr(part, "inline_data", None)
            if inline and inline.data and (inline.mime_type or "").startswith("image/"):
                return inline.data
    # No pixels back, and until this the error said only that. Which of the
    # reasons it is -- input-side filtering, output-side filtering, or a model
    # that simply answered in prose -- lives in the finish_reason, in
    # prompt_feedback, or in the text part it sent instead of an image, and
    # all three were being discarded here. It is NOT safe to assume the edit
    # itself was refused: the same instruction on a neighbouring shot came
    # back edited and passed the modesty check minutes earlier, so an empty
    # response is a fact about one request, not about the class of edit.
    candidates = response.candidates or []
    said = " ".join(
        (getattr(part, "text", "") or "").strip()
        for candidate in candidates
        for part in (getattr(getattr(candidate, "content", None), "parts", None) or [])
    ).strip()
    detail = "; ".join(x for x in (
        ", ".join(str(getattr(c, "finish_reason", "") or "") for c in candidates
                  if getattr(c, "finish_reason", None)),
        str(getattr(getattr(response, "prompt_feedback", None),
                    "block_reason", "") or ""),
        said[:300],
    ) if x)
    raise RemediationError(
        f"{settings.model_image} returned no image part for this edit"
        + (f" ({detail})" if detail else "")
    )

def _compliant_line(finding: Finding, market_name: str) -> str:
    """Write the replacement voice-over line for a revoice."""
    rule = _rule_for(finding)
    prompt = _LINE_PROMPT.format(
        market_name=market_name,
        rule_basis=rule.basis if rule else finding.citation_ref,
        rationale=finding.rationale,
    )
    raw = generate_json(settings.model_text, [prompt], _LINE_SCHEMA)
    line = raw.get("line") if isinstance(raw, dict) else None
    if not isinstance(line, str) or not line.strip():
        raise RemediationError("the model returned no replacement line")
    return line.strip()

def _speak(line: str) -> tuple[bytes, int]:
    """Render one line with the TTS model. Returns (raw PCM s16le mono, rate)."""
    config = types.GenerateContentConfig(
        response_modalities=["AUDIO"],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=TTS_VOICE))),
    )
    response = client().models.generate_content(
        model=settings.model_tts,
        contents=f"Read this advertising line in a confident, upbeat announcer voice: {line}",
        config=config,
    )
    for candidate in (response.candidates or []):
        for part in (getattr(candidate.content, "parts", None) or []):
            inline = getattr(part, "inline_data", None)
            if inline and inline.data:
                match = _PCM_RATE_RE.search(inline.mime_type or "")
                rate = int(match.group(1)) if match else _TTS_DEFAULT_RATE
                return inline.data, rate
    raise RemediationError(f"{settings.model_tts} returned no audio for this line")

def _write_wav(pcm: bytes, rate: int, out_path: Path) -> Path:
    """Wrap raw mono s16le PCM in a WAV header. stdlib, no ffmpeg hop needed:
    media.replace_audio_span resamples whatever it is given anyway."""
    with wave.open(str(out_path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(pcm)
    return out_path

# --- the edit itself ---

def _still(video_path, change_id: str, tag: str, finding: Finding, out_dir: Path) -> Path:
    """One still from the middle of the finding's span, into runs/{run}/changes/.

    Before and after are pulled at the same timestamp from the same span, so
    the pair is directly comparable -- that is the whole point of a change
    record a human can check.
    """
    span = Shot(shot_id=f"{change_id}_{tag}", t_start=finding.t_start, t_end=finding.t_end)
    return media.extract_keyframes(video_path, span, out_dir, per_shot=1)[0]

def _siblings_for(finding: Finding, store) -> list[Finding]:
    """The other open findings on this shot, or [] when we cannot ask."""
    if store is None:
        return []
    try:
        return siblings_in_shot(finding, store.findings(finding.run_id, finding.market))
    except Exception as exc:  # noqa: BLE001 -- grouping is an optimisation
        log.warning("sibling lookup failed for %s: %s", finding.id, exc)
        return []


def _box_for(finding: Finding, before: Path, store=None) -> list:
    """Where in the frame this finding is, or [] if nobody can say.

    Three sources in order of cost: the finding (written at judging time),
    the observation it came from, and one locate() call against the
    evidence frame for findings that predate boxes entirely.
    """
    box = list(getattr(finding, "box", None) or [])
    if box:
        return box
    if store is not None:
        obs = next((o for o in store.observations(finding.run_id)
                    if o.id == finding.observation_id), None)
        if obs is not None and getattr(obs, "box", None):
            return list(obs.box)
    if before.is_file():
        try:
            from customs import analyst
            return list(analyst.locate(before.read_bytes(), finding.rationale) or [])
        except Exception as exc:  # noqa: BLE001 -- no box is a fallback, not a failure
            log.warning("locate failed for %s: %s", finding.id, exc)
    return []


def _land_edit(base: Path, before: Path, edited: Path, finding: Finding,
               workdir: Path, out_path: Path, box: list,
               landing: str | None = None) -> str:
    """Get an edited still onto the master, and say how it was done.

    With a box, the edit is landed as a RELIGHT: the ratio edited/original
    is computed inside the box and multiplied into every live frame of the
    span. The span keeps its own motion, lighting and grain, and only the
    object's surface changes -- for the price of the one image edit that
    was already paid for.

    Without a box there is nothing to constrain the edit to, so it falls
    back to what this always did: hold the edited still over the span.
    That is a freeze frame, and it is why a box is worth locating.

    `landing="freeze"` forces the freeze: it is what the console's "Patch
    one frame" promises, and the picker's word is kept even when a box
    exists. Anything else means choose as above -- which is exactly what
    "Propagate the change" promises when a box exists.
    """
    if landing == "freeze":
        media.overlay_image(base, edited, finding.t_start, finding.t_end, out_path)
        return "freeze"
    if box:
        ratio = media.relight_ratio(before, edited, box,
                                    workdir / f"{before.stem}_ratio.png")
        media.apply_relight(base, ratio, box,
                            finding.t_start, finding.t_end, out_path)
        return "relight"
    media.overlay_image(base, edited, finding.t_start, finding.t_end, out_path)
    return "freeze"


def _edit_frame_onto(base: Path, finding: Finding, method: str, replacement: str | None,
                     before: Path, workdir: Path, out_path: Path,
                     intent: str | None = None, store=None,
                     landing: str | None = None) -> tuple[Path, str]:
    """relettering / prop_swap: edit the keyframe, fit it, composite the span."""
    market_name = _market_name(finding.market)
    siblings = _siblings_for(finding, store)
    directive = _directive_for(finding, intent)
    if directive and not siblings:
        # an intent with its own directive replaces the whole instruction
        instruction = (f"Edit this frame from a television commercial. {directive} "
                       f"Keep the lighting, the composition, the camera angle and "
                       f"every other pixel of the frame identical. Add nothing "
                       f"that is not already in the frame. The market is "
                       f"{market_name}.")
        if siblings:
            instruction = _combined_directive(finding, siblings, replacement,
                                              intent, market_name)
        edited_raw = workdir / f"{before.stem}_edited_raw.png"
        edited_raw.parent.mkdir(parents=True, exist_ok=True)
        edited_raw.write_bytes(_edit_image(instruction, before.read_bytes()))
        edited = media.fit_image(edited_raw, base,
                                 before.with_name(f"{before.stem}_edited.png"))
        _land_edit(base, before, edited, finding, workdir, out_path,
                   _box_for(finding, before, store), landing=landing)
        return edited, instruction
    if replacement is None and \
            METHOD_BY_DIMENSION.get(_dimension_of(finding, None)) != method:
        # The method fell back here from an unmapped dimension, and this
        # path then reached for _DEFAULT_REPLACEMENT[method] -- the alcohol
        # substitution -- which is how a modesty finding was sent "replace
        # each alcoholic drink": once as the green can, and again, live, on
        # the 1984 runner the day prop_swap became the fallback method.
        # _frame_instruction walks the honest ladder instead: the intent's
        # directive, the dimension's own suggested remedy, then a generic
        # change-what-the-market-objects-to edit.
        instruction = _frame_instruction(finding, None, intent, market_name)
    else:
        if replacement is None:
            subject = _DEFAULT_REPLACEMENT[method].format(market_name=market_name)
        elif method == "relettering":
            subject = f'the text "{replacement}"'
        else:
            subject = replacement
        instruction = _EDIT_INSTRUCTIONS[method].format(replacement=subject)
        if method == "prop_swap" and market_name not in instruction:
            instruction = f"{instruction} The market is {market_name}."
    if siblings:
        instruction = _combined_directive(finding, siblings, replacement,
                                          intent, market_name)

    edited_raw = workdir / f"{before.stem}_edited_raw.png"
    edited_raw.parent.mkdir(parents=True, exist_ok=True)
    edited_raw.write_bytes(_edit_image(instruction, before.read_bytes()))
    # the model picks its own output resolution; the composite needs the
    # master's exact pixel size or the still would cover only part of it.
    edited = media.fit_image(edited_raw, base, before.with_name(f"{before.stem}_edited.png"))

    _land_edit(base, before, edited, finding, workdir, out_path,
               _box_for(finding, before, store), landing=landing)
    return edited, instruction

_GENERIC_EDIT = (
    "Edit this frame from a television commercial. {directive} Keep the "
    "lighting, the composition, the camera angle, the performers and every "
    "other pixel of the frame identical. Add nothing that is not already in "
    "the frame, and do not write any text into the picture. The market is "
    "{market_name}."
)


def siblings_in_shot(finding: Finding, findings: list[Finding]) -> list[Finding]:
    """The other open findings this market holds on the same shot.

    Findings inherit their span from the shot, so a shot routinely carries
    several. Measured on a real run: SA span 35.083-42.083 holds three.
    Fixing them one at a time regenerated the same seven seconds three
    times, at 3.68 EUR each, and every pass after the first took its
    anchors from the previous pass's OUTPUT -- Veo generating from Veo,
    which is the compounding that turns a hammer into a baseball bat.

    Matched by shot when both carry one, and by identical span otherwise,
    which is what a shared shot looked like before shot_id existed.
    """
    mine = getattr(finding, "shot_id", "") or ""
    span = (round(finding.t_start, 3), round(finding.t_end, 3))
    out = []
    for other in findings:
        if other.id == finding.id or other.status != "open":
            continue
        if other.market != finding.market or other.remediation_blocked:
            continue
        theirs = getattr(other, "shot_id", "") or ""
        same = (mine and theirs and mine == theirs) or \
               (round(other.t_start, 3), round(other.t_end, 3)) == span
        if same:
            out.append(other)
    return out


def _combined_directive(finding: Finding, siblings: list[Finding],
                        replacement: str | None, intent: str | None,
                        market_name: str) -> str:
    """One instruction that deals with every violation in this shot.

    The alternative is one edit per finding, each starting from the last
    one's output. That is three generations of loss on the same seconds,
    and each pass is judged against a frame the previous pass invented.

    The targeted finding leads, because it is the one the operator chose
    and the one whose intent and replacement they picked. The others are
    appended as plain additional requirements.
    """
    lead = _frame_instruction(finding, replacement, intent, market_name)
    if not siblings:
        return lead
    extra = []
    for other in siblings:
        directive = _directive_for(other, None)
        if not directive:
            remedies = getattr(other, "remedies", None) or []
            directive = (remedies[0].get("directive") if remedies else None)
        extra.append(directive or f"Also resolve: {other.rationale}")
    return (f"{lead} In the same frame, also make these changes, and change "
            f"nothing else: " + " ".join(extra))


def _frame_instruction(finding: Finding, replacement: str | None,
                       intent: str | None, market_name: str) -> str:
    """What to tell the image model, for THIS finding.

    _bridge_span used to hardcode _DEFAULT_REPLACEMENT["prop_swap"] -- the
    alcohol substitution -- whatever the finding was about, and _run_method
    never passed it the operator's intent. So a Saudi modesty finding ("a
    sleeveless halter top displaying bare shoulders and upper arms") was
    remediated by instructing the model to "replace each alcoholic drink
    with a non-alcoholic drink that suits Saudi Arabia". There was no drink
    in the shot, so the model invented one: a green can. Nobody ever asked
    for sleeves, so the anchors went to Veo still sleeveless, and Veo
    faithfully interpolated between two frames that both had a can in them.

    The order here is: what the operator picked, then what they typed, then
    the dimension's own default -- and only alcohol, food and text have a
    substitution default, because only those are substitutions.
    """
    directive = _directive_for(finding, intent)
    if directive:
        return _GENERIC_EDIT.format(directive=directive, market_name=market_name)

    # Gate on the dimension's OWN mapping, not on plan(): plan() now answers
    # prop_swap for every unmapped dimension (the crop fallback is gone), and
    # letting that reach the substitution default re-created the green can --
    # a modesty finding sent "replace each alcoholic drink". Only the
    # dimensions that really are substitutions get the substitution text.
    mapped = METHOD_BY_DIMENSION.get(_dimension_of(finding, None))
    if replacement is not None or mapped in _DEFAULT_REPLACEMENT:
        key = mapped if mapped in _DEFAULT_REPLACEMENT else "prop_swap"
        subject = replacement or _DEFAULT_REPLACEMENT[key].format(market_name=market_name)
        return _EDIT_INSTRUCTIONS[key].format(replacement=subject)

    # Nothing chosen and no substitution default: take the first remedy this
    # dimension offers that actually carries a directive. costs.suggestions
    # prefers the judge's own three, which were written against this frame.
    from customs import costs as _costs
    for option in _costs.suggestions(_dimension_of(finding, None), finding):
        directive = _directive_for(finding, option["key"])
        if directive:
            return _GENERIC_EDIT.format(directive=directive, market_name=market_name)

    return _GENERIC_EDIT.format(
        directive=("Change only what the market objects to in this frame and "
                   "leave the rest of the picture exactly as it is."),
        market_name=market_name)


_CHECK_SCHEMA = {
    "type": "object",
    "properties": {
        "fixed": {"type": "boolean"},
        "still_visible": {"type": "string"},
        "added": {"type": "string"},
    },
    "required": ["fixed", "still_visible", "added"],
}

_CHECK_PROMPT = (
    "You are checking one frame of a television commercial that has just been "
    "edited to satisfy a broadcast rule.\n\n"
    "THE RULE: {trigger}\n"
    "WHAT WAS ORIGINALLY OBSERVED IN THIS FRAME: {statement}\n"
    "WHAT THE EDIT WAS ASKED TO DO: {directive}\n\n"
    "fixed: true only if the frame as it now stands no longer breaks that "
    "rule. Judge the picture in front of you, not the instruction: an edit "
    "that changed something else, or changed nothing, is not fixed. Judge "
    "as the market's most conservative reviewer would, and judge EVERY "
    "problem listed above: exposure that was reduced but is still visible "
    "is not fixed, and one problem solved while another remains is not "
    "fixed.\n"
    "still_visible: if it is not fixed, name what still breaks the rule, in "
    "a few words. Empty string if it is fixed.\n"
    "added: name anything present that the description does not account for "
    "and that has no reason to be in this scene -- a drink, a prop, an "
    "object that does not belong. Empty string if nothing was added."
)


def _note(on_event, message: str) -> None:
    if on_event is not None:
        on_event("remediator", message)


def _anchor_check(image_bytes: bytes, finding: Finding, statement: str,
                  directive: str, siblings: list | None = None) -> dict:
    """Ask a cheap model whether the edited frame actually complies.

    A Veo bridge costs about EUR 1.88 and takes minutes; this is one image
    call. Sending an anchor to Veo without looking at it is how a modesty
    finding came back as a green drinking can, four generated seconds and a
    charge, for a frame that never had its sleeves changed.
    """
    rule = _rule_for(finding)
    observed = statement or "(not recorded)"
    # The check judges the GROUP: an edit that satisfied the lead finding
    # while a sibling's problem stayed on screen was passed here as
    # "compliant" and then honestly failed by the verifier -- sleeves were
    # added, the short shorts stayed, 1.88 EUR bought a reopened finding.
    for other in (siblings or []):
        if other.rationale:
            observed += f"\nALSO OBSERVED IN THIS SHOT: {other.rationale}"
    prompt = _CHECK_PROMPT.format(
        trigger=(rule.trigger if rule else finding.rationale) or "the market's rule",
        statement=observed,
        directive=directive,
    )
    try:
        return generate_json_image(prompt, image_bytes, _CHECK_SCHEMA)
    except Exception:  # noqa: BLE001 -- a checker that breaks must not block a fix
        return {"fixed": True, "still_visible": "", "added": "",
                "unchecked": True}


def _per_frame_span(base: Path, finding: Finding, replacement: str | None,
                    workdir: Path, out_path: Path,
                    intent: str | None = None, store=None,
                    spend=None, on_event=None) -> tuple[Path, str]:
    """Repaint the offending region on every frame, keeping the frames.

    The last resort before regeneration, and the only one that reaches a
    target which deforms or is occluded -- a bottle travelling to a
    performer's lips through her own hand, a garment on a moving body --
    where one edit cannot be propagated because the thing does not hold
    still to be propagated onto.

    Every frame is the film's own. The model only ever sees, and only
    ever returns, the frame it is editing; the composite then puts back
    just the finding's box, at PNG level, so every pixel outside it is
    bit-identical rather than merely close.

    Each frame is edited with the PREVIOUS edited frame alongside it as a
    labelled reference. Independent edits of consecutive frames diverge --
    measured, two edits of a byte-identical frame differ by 16.6 MAD
    inside the box -- and that divergence at 12fps is a flicker. Chaining
    makes it serial, which is why this is slow and priced by the frame.
    """
    box = _box_for(finding, _still(base, "pf", "probe", finding, workdir), store)
    if not box:
        raise RemediationError(
            "per-frame editing needs to know where in the frame to look, and "
            "nothing could locate this finding")

    frames_dir = workdir / f"pf_{finding.id}_src"
    edited_dir = workdir / f"pf_{finding.id}_out"
    edited_dir.mkdir(parents=True, exist_ok=True)
    frames = media.extract_span_frames(base, finding.t_start, finding.t_end,
                                       frames_dir, fps=costs.PER_FRAME_FPS)
    if not frames:
        raise RemediationError("no frames extracted for the span")

    market_name = _market_name(finding.market)
    instruction = _frame_instruction(finding, replacement, intent, market_name)
    if spend is not None:
        spend(costs.estimate("per_frame", finding.t_end - finding.t_start))

    # One image call per frame, against a quota of two per minute, is the one
    # method the pacing in _edit_image cannot hide: a four second span is
    # around half an hour of mostly waiting. Say so before starting rather
    # than leaving an operator watching a progress line crawl -- the number
    # is what tells them to raise the quota or pick another method.
    if len(frames) > 2:
        minutes = len(frames) * _IMAGE_MIN_INTERVAL_S / 60
        _note(on_event, f"{len(frames)} frames at two image generations per "
                        f"minute: about {minutes:.0f} minutes of pacing")

    previous: bytes | None = None
    for i, frame in enumerate(frames):
        if on_event and i % 8 == 0:
            on_event("remediator",
                     f"repainting frame {i + 1} of {len(frames)}")
        # _edit_image already labels a chained reference correctly, and it
        # exists for exactly this reason on the bridge's two anchors: two
        # independent edits of the same object are two different objects.
        raw = edited_dir / f"raw_{frame.name}"
        raw.write_bytes(_edit_image(instruction, frame.read_bytes(),
                                    reference=previous))
        previous = raw.read_bytes()
        media.paste_box(frame, raw, box, edited_dir / frame.name)

    media.replace_segment_video(base, finding.t_start, finding.t_end,
                                edited_dir, out_path)
    return frames[0], instruction


def _omni_span(base: Path, finding: Finding, replacement: str | None,
               workdir: Path, out_path: Path,
               keep_dir: Path | None = None,
               intent: str | None = None,
               change_id: str = "", statement: str = "",
               spend=None, on_event=None, store=None) -> tuple[Path, str]:
    """Hand the span itself to Gemini Omni and splice its rewrite back.

    The other expensive tier, and the opposite bet to the bridge: the
    bridge regenerates pixels between two edited stills, Omni edits the
    moving footage directly -- same shots, same motion, only what the
    instruction names changes. Cheaper per second than Veo, capped at 10
    input seconds by Google, and subject to the same group contract as
    every other method: one edit answers every open finding on the shot.
    """
    market_name = _market_name(finding.market)
    siblings = _siblings_for(finding, store)
    directive = _combined_directive(finding, siblings, replacement, intent,
                                    market_name)
    instruction = (f"Edit this video. {directive} Keep everything else "
                   f"exactly the same: same shots, same motion, same "
                   f"performers, same lighting, same duration. Change "
                   f"nothing that was not named.")

    clip = media.cut_span(base, finding.t_start, finding.t_end,
                          workdir / f"omni_{finding.id}_in.mp4")
    try:
        edited = generate_omni_edit(instruction, clip,
                                    workdir / f"omni_{finding.id}_out.mp4")
    except OmniQuota as exc:
        raise RemediationError(str(exc)) from exc

    # Charged only for footage that exists, like the bridge.
    if spend is not None:
        spend()
    if keep_dir is not None and change_id:
        keep_dir.mkdir(parents=True, exist_ok=True)
        try:
            (keep_dir / f"{change_id}_omni.mp4").write_bytes(edited.read_bytes())
        except OSError:
            pass
    media.splice_clip(base, edited, finding.t_start, finding.t_end, out_path)
    _note(on_event, f"omni rewrote {finding.t_end - finding.t_start:.1f}s in place")
    return edited, instruction


def _bridge_span(base: Path, finding: Finding, replacement: str | None,
                 workdir: Path, out_path: Path,
                 keep_dir: Path | None = None,
                 intent: str | None = None,
                 change_id: str = "", statement: str = "",
                 spend=None, on_event=None, store=None) -> tuple[Path, str]:
    """Edit both ends of the span and let Veo generate the motion between.

    The only method that can follow genuine 3D motion, and the only one that
    puts pixels on screen the brand never shot, so it is the expensive tier
    and the console prices it before anyone presses the button. Both anchors
    are edited with the same instruction the patch methods use, so the
    generated motion starts and ends on frames that are already compliant.
    """
    market_name = _market_name(finding.market)
    # The GROUP's instruction, not the lead finding's. _apply_locked has
    # already announced "one edit for N findings on this shot" and set the
    # siblings remediating -- but this function used to edit for the lead
    # alone, so a 1.88 EUR bridge dressed the tank top and left the short
    # shorts on screen, and the verifier honestly reopened the finding.
    # _combined_directive returns the plain lead instruction when the shot
    # holds nothing else.
    siblings = _siblings_for(finding, store)
    instruction = _combined_directive(finding, siblings, replacement, intent,
                                      market_name)

    # The tail is edited against the head's result, not on its own. Two
    # independent edits answer the same question twice and rarely identically,
    # and Veo spends the span morphing one answer into the other -- which
    # looks exactly like the artefact it is. Chained, both ends hold the same
    # object and Veo only has to move it.
    def _make_anchors(extra: str = "") -> list[Path]:
        """Both anchors, edited, checked and fitted.

        `extra` re-phrases the edit toward the same goal. It exists because
        Veo's input filter sometimes refuses frames Gemini's editor happily
        produced and our own check passed -- and the second try then has to
        be a different PICTURE, not different words to Veo.
        """
        built: list[Path] = []
        first_edit: bytes | None = None
        base_instruction = instruction + extra
        for tag, when in (("head", finding.t_start), ("tail", max(finding.t_start, finding.t_end - 0.08))):
            span = Shot(shot_id=f"bridge_{tag}", t_start=when, t_end=when + 0.04)
            raw = media.extract_keyframes(base, span, workdir / "bridge", per_shot=1)[0]

            # Check the anchor before it is worth anything. Veo costs about
            # EUR 1.88 and several minutes; this is one image call. One retry,
            # told what went wrong, then give up rather than generate four
            # seconds of motion between two frames that never got fixed.
            attempt_instruction = base_instruction
            for attempt in (1, 2):
                edited_bytes = _edit_image(attempt_instruction, raw.read_bytes(),
                                           reference=first_edit)
                verdict = _anchor_check(edited_bytes, finding, statement,
                                        base_instruction, siblings=siblings)
                if verdict.get("unchecked"):
                    _note(on_event, f"bridge {tag}: could not be checked, proceeding")
                    break
                if verdict.get("fixed") and not (verdict.get("added") or "").strip():
                    _note(on_event, f"bridge {tag}: checked, compliant")
                    break
                problem = ", ".join(x for x in (verdict.get("still_visible"),
                                                verdict.get("added")) if x)
                if attempt == 2:
                    raise RemediationError(
                        f"the edited {tag} frame still does not satisfy "
                        f"{finding.rule_id}: {problem or 'unchanged'}. Nothing was "
                        f"generated, so nothing was charged.")
                _note(on_event, f"bridge {tag}: rejected ({problem}), retrying")
                attempt_instruction = (
                    f"{base_instruction} A previous attempt failed because: {problem}. "
                    f"Fix that specifically, and do not add anything to the scene.")

            if first_edit is None:
                first_edit = edited_bytes
            edited_raw = workdir / f"bridge_{tag}_edited_raw.png"
            edited_raw.write_bytes(edited_bytes)
            fitted = media.fit_image(edited_raw, base, workdir / f"bridge_{tag}.png")
            built.append(fitted)
            # Kept beside the change record, not left in the scratch workdir,
            # which is never mirrored. These two frames ARE the brief Veo was
            # given: everything in the generated seconds is supposed to trace
            # back to them, so they are the only way to judge whether Veo
            # invented something or was handed it.
            if keep_dir is not None and change_id:
                keep_dir.mkdir(parents=True, exist_ok=True)
                try:
                    (keep_dir / f"{change_id}_anchor_{tag}.png").write_bytes(
                        fitted.read_bytes())
                except OSError:
                    pass
        return built

    anchors = _make_anchors()

    seconds = costs.bridge_seconds(finding.t_end - finding.t_start)
    # _BRIDGE_PROMPT, not `instruction`: the anchors already carry the swap,
    # and telling a video model to "replace every alcoholic drink with tea"
    # makes it add tea to the scene rather than leave the corrected frames
    # alone. See _BRIDGE_PROMPT.
    # Veo's safety filter is stochastic: the same two frames can be accepted
    # on a second attempt. A blocked generation is free, so retrying one
    # costs nothing and often works.
    def _generate(attempt: int):
        return generate_bridge(
            prompt=_BRIDGE_PROMPT,
            first_frame=anchors[0], last_frame=anchors[1],
            seconds=seconds, out_path=workdir / f"bridge{attempt}.mp4")

    clip = None
    blocked_tries = 0
    redressed = False
    attempt = 0
    while clip is None:
        attempt += 1
        try:
            clip = _generate(attempt)
        except VeoBlocked as exc:
            # Google does not charge for a blocked video, so neither do we.
            blocked_tries += 1
            _note(on_event, f"bridge: Veo's safety filter rejected the "
                            f"generation (attempt {blocked_tries}/2), not charged")
            if blocked_tries == 2:
                raise RemediationError(
                    "Veo's safety filter rejected this generation twice. "
                    "Nothing was charged. Try a different remedy, or patch "
                    "the frame instead of regenerating the shot.") from exc
        except VeoRefusedInput as exc:
            # The INPUT gate, not the output one: Veo would not take the
            # anchor frames at all, so zero seconds were generated and
            # nothing is charged. Retrying the same frames is pointless --
            # the fix is a different picture with the same goal, so both
            # anchors are re-edited more conservatively and Veo gets one
            # more look. New anchors also mean a new derived seed.
            #
            # Unless the refusal is about WHO is in the frame. A celebrity
            # or child code means Veo recognised a face it will not animate
            # -- the Chanel spot refused Keira Knightley's likeness, code
            # 15236754 -- and no amount of re-dressing changes a face, so
            # the two image edits a redress costs would be pure waste.
            identity = [c for c in getattr(exc, "categories", ())
                        if "celebrity" in c or "child" in c]
            if identity:
                raise RemediationError(
                    f"Veo will not generate video of this scene: its input "
                    f"filter flagged {' and '.join(identity)} in the anchor "
                    f"frames, and that is about who is on screen, not how "
                    f"they are dressed. Nothing was generated, so nothing "
                    f"was charged. Patch the frame instead -- the patch "
                    f"methods never go through Veo.") from exc
            if redressed:
                raise RemediationError(
                    "Veo refused the anchor frames twice, even re-dressed "
                    "more conservatively: its input filter will not take "
                    "this scene. Nothing was generated, so nothing was "
                    "charged. Patch the frame instead -- the patch methods "
                    "never go through Veo.") from exc
            redressed = True
            _note(on_event, "bridge: Veo refused the anchor frames (input "
                            "filter); re-editing both more conservatively "
                            "and trying once more, not charged")
            anchors = _make_anchors(_ANCHOR_RETRY_EXTRA)
        except Exception:
            # Any other failure means the generation was attempted and
            # billed, whether or not it came back.
            if spend is not None:
                spend()
            raise

    # Charged once, for a generation that actually happened.
    if spend is not None:
        spend()
    # Keep the generated footage itself, not just the master it went into.
    # The scratch workdir is deliberately not mirrored, so a clip left there
    # is gone the next time the container is replaced, and "nothing is edited
    # silently" has to cover the seconds a model invented most of all: they
    # are the ones a brand will want to watch before signing anything.
    # Named for the change record, which is what the download route asks
    # for. This used to derive the name from out_path -- the staged file,
    # whose stem starts ".chg" -- so every bridge in a run wrote one hidden
    # file, ".chg_bridge.mp4": the link 404'd every time, and each new
    # generation silently overwrote the one before it.
    if keep_dir is not None and change_id:
        keep_dir.mkdir(parents=True, exist_ok=True)
        kept = keep_dir / f"{change_id}_bridge.mp4"
        try:
            kept.write_bytes(Path(clip).read_bytes())
        except OSError:
            pass
    # Spliced whole. This was matted for a while -- only the finding's own
    # box of the generated frame reaching the master, so that Veo could not
    # repaint the performance around the object ("the hammer that came back
    # as a baseball bat"). Watched on a real spot, that is worse than the
    # problem it solves: Veo reinterprets the whole frame, so a box cut out
    # of its version and pasted over the original lines up with nothing
    # around it and reads as a hard-edged window of a different film
    # sitting in the middle of the shot. A bridge regenerates the span; it
    # regenerates the whole of it.
    media.splice_clip(base, clip, finding.t_start, finding.t_end, out_path)
    return anchors[0], instruction


def apply(run, finding: Finding, method: str, workdir, store, *,
          replacement: str | None = None, intent: str | None = None,
          statement: str = "", spend=None, on_event=None,
          landing: str | None = None) -> ChangeRecord:
    """Apply one remediation to this market's localized master.

    Args:
        run: the RunRecord the finding belongs to.
        finding: what to fix. Re-checked against the guard's decision first.
        method: one of METHODS (plan() chooses it).
        workdir: scratch directory for extracted frames and raw model output.
        store: the run store. Also fixes where runs/{run_id}/ lives.
        replacement: the exact replacement to use -- the translated line for
            relettering, the prop to swap in for prop_swap, the spoken line
            for revoice. Left out, the model decides it from the market and
            the finding (which is what an unattended alert-driven remediation
            does); passed in, it is used verbatim, which is what the demo's
            scripted French relettering does.

    Returns the persisted ChangeRecord.

    Serialized per (run_id, market) by market_lock: the master is read,
    edited and replaced here, so two alerts for one market must not interleave.

    The finding is left at status "remediating", never "resolved": only
    verify.confirm may resolve it, after re-observing the edited master. A
    failed edit raises with the master untouched and the finding put straight
    back to "open" (design spec section 14: "remediation failure leaves the
    original media untouched and the alert unresolved") -- which is why every
    method writes to a temporary file and only replaces the master once
    ffmpeg has returned, and why the status restore is not optional: a
    finding stuck at "remediating" would be invisible to clearance() and the
    market would look fixed when nothing was.
    """
    _refuse_if_blocked(finding)
    if method not in METHODS:
        raise ValueError(f"unknown remediation method: {method!r} (expected one of {list(METHODS)})")

    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    changes_dir = run_dir(run, store) / "changes"
    changes_dir.mkdir(parents=True, exist_ok=True)

    with market_lock(run.id, finding.market):
        return _apply_locked(run, finding, method, workdir, store, intent,
                             replacement, changes_dir, statement=statement,
                             spend=spend, on_event=on_event, landing=landing)

def _apply_locked(run, finding: Finding, method: str, workdir: Path, store,
                  intent: str | None,
                  replacement: str | None, changes_dir: Path,
                  statement: str = "", spend=None, on_event=None,
                  landing: str | None = None) -> ChangeRecord:
    """apply()'s body, with this market's master already locked."""
    master = localized_master(run, finding.market, store)
    base = master if master.exists() else Path(run.asset_path)
    change_id = f"chg_{uuid.uuid4().hex[:12]}"
    store.emit(run.id, "remediator",
               f"{method} -> {finding.id} ({finding.rule_id}, {finding.market}) "
               f"on {base.name} at {finding.t_start:.2f}-{finding.t_end:.2f}s")
    store.update_finding_status(finding.id, "remediating", run_id=run.id)

    # Everything else this market holds open on the same shot goes with it.
    # One shot, one edit: fixing them one at a time regenerated the same
    # seconds once per finding, each pass taking its anchors from the last
    # pass's output. The verifier rules on all of them afterwards and puts
    # back any that survived.
    company = _siblings_for(finding, store)
    for other in company:
        store.update_finding_status(other.id, "remediating", run_id=run.id)
    if company:
        store.emit(run.id, "remediator",
                   f"one edit for {1 + len(company)} findings on this shot: "
                   f"{finding.rule_id} plus "
                   f"{', '.join(o.rule_id for o in company)}")

    before = _still(base, change_id, "before", finding, changes_dir)
    staged = run_dir(run, store) / f".{change_id}_{master.name}"

    try:
        edit = _run_method(run, finding, method, replacement, intent, base, before,
                           workdir, staged, store, changes_dir, change_id,
                           statement=statement, spend=spend, on_event=on_event,
                           landing=landing)
    except Exception:
        # A half-done remediation must never look like a done one. The finding
        # goes straight back to open, so clearance() counts it again and the
        # alert stays up: design spec section 14, "remediation failure leaves
        # the original media untouched and the alert unresolved". The master is
        # untouched by construction -- every method writes to `staged` and only
        # the line after this block replaces the master with it.
        store.update_finding_status(finding.id, "open", run_id=run.id)
        for other in company:
            store.update_finding_status(other.id, "open", run_id=run.id)
        staged.unlink(missing_ok=True)
        store.emit(run.id, "remediator",
                   f"stage_error: remediate: {method} failed for {finding.id}; "
                   f"{master.name} untouched, finding back to open")
        raise

    # The craft gate. verify.confirm asks whether the RULE still fires; it
    # has never asked whether the commercial survived. A measured
    # remediation chain silently lost 15 frames of running length, which
    # for a broadcast spot is a broken deliverable however good the picture
    # looks. So the staged file is interrogated BEFORE it becomes the
    # master, and a failure puts the finding back exactly like any other
    # remediation failure: the master is untouched and the alert stays up.
    craft = media.craft_check(base, staged,
                              span=(finding.t_start, finding.t_end))
    if craft["failures"]:
        store.update_finding_status(finding.id, "open", run_id=run.id)
        for other in company:
            store.update_finding_status(other.id, "open", run_id=run.id)
        staged.unlink(missing_ok=True)
        for failure in craft["failures"]:
            store.emit(run.id, "remediator", f"craft gate: {failure}")
        store.emit(run.id, "remediator",
                   f"stage_error: remediate: {method} produced a master that is not "
                   f"the same film; {master.name} untouched, {finding.id} back to open")
        raise RemediationError(
            f"{method} failed the craft gate: {'; '.join(craft['failures'])}")
    store.emit(run.id, "remediator",
               f"craft gate passed: length {craft['duration_after']:.3f}s, "
               f"{craft['frames_after']} frames, "
               f"{craft['psnr']:.1f} dB outside the edit"
               if craft.get("psnr") is not None else
               f"craft gate passed: length {craft['duration_after']:.3f}s, "
               f"{craft['frames_after']} frames")

    staged.replace(master)
    after = _still(master, change_id, "after", finding, changes_dir)

    change = ChangeRecord(
        id=change_id, run_id=run.id, finding_id=finding.id, method=method,
        description=edit, before_frame=str(before), after_frame=str(after),
    )
    store.add_change(change)
    store.emit(run.id, "remediator",
               f"{method} applied -> {master.name} ({change.id}); {edit}")
    return change

def _run_method(run, finding: Finding, method: str, replacement: str | None,
                intent: str | None,
                base: Path, before: Path, workdir: Path, staged: Path, store,
                changes_dir: Path | None = None, change_id: str = "",
                statement: str = "", spend=None, on_event=None,
                landing: str | None = None) -> str:
    """Produce the edited video at `staged` and return the change description.

    Split out of apply() so every failure mode of every method funnels through
    one try/except there, rather than each method having to remember to put
    the finding back.
    """
    market_name = _market_name(finding.market)
    if method in ("relettering", "prop_swap"):
        _edited, instruction = _edit_frame_onto(
            base, finding, method, replacement, before, workdir, staged,
            intent=intent, store=store, landing=landing)
        # The full edit instruction goes in the run's event log, not in the
        # change description: the description is read on dashboards and in
        # Grafana annotations, where a paragraph of prompt is noise, but
        # "nothing is edited silently" still means the exact instruction has
        # to be recoverable from the run.
        store.emit(run.id, "remediator", f"{method} instruction: {instruction}")
        if method == "relettering":
            return (
                f'relettered the on-screen text as "{replacement}"' if replacement
                else f"relettered the on-screen text for {market_name}"
            )
        return (
            f"swapped the prop for {replacement}" if replacement
            else f"swapped the non-compliant prop for a {market_name}-appropriate one"
        )
    if method == "revoice":
        line = replacement or _compliant_line(finding, market_name)
        pcm, rate = _speak(line)
        wav = _write_wav(pcm, rate, workdir / f"{staged.stem}.wav")
        media.replace_audio_span(base, wav, finding.t_start, finding.t_end, staged)
        return f'replaced the spoken line with "{line}"'
    if method == "per_frame":
        _edited, instruction = _per_frame_span(
            base, finding, replacement, Path(workdir), staged,
            intent=intent, store=store, spend=spend, on_event=on_event)
        store.emit(run.id, "remediator", f"per_frame instruction: {instruction}")
        span = finding.t_end - finding.t_start
        return (f"repainted the offending region on "
                f"{costs.per_frame_edits(span)} frames of the span")
    if method == "omni":
        _edited, instruction = _omni_span(base, finding, replacement,
                                          Path(workdir), staged,
                                          keep_dir=changes_dir, intent=intent,
                                          change_id=change_id, statement=statement,
                                          spend=spend, on_event=on_event,
                                          store=store)
        store.emit(run.id, "remediator", f"omni instruction: {instruction}")
        return (f"rewrote the span in place with Omni; the footage is the "
                f"brand's own throughout")
    if method == "bridge":
        _edited, instruction = _bridge_span(base, finding, replacement,
                                            Path(workdir), staged,
                                            keep_dir=changes_dir, intent=intent,
                                            change_id=change_id, statement=statement,
                                            spend=spend, on_event=on_event,
                                            store=store)
        return (f"regenerated {costs.bridge_seconds(finding.t_end - finding.t_start):.0f}s "
                f"of motion between two edited anchor frames")
    # Unreachable while the METHODS guard above holds: every member has an
    # explicit branch. The old fall-through here was the reframe centre
    # crop, removed 2026-08-31 for being useless in practice.
    raise ValueError(f"unhandled method: {method}")
