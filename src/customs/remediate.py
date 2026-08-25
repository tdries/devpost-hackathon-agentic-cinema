"""The Remediator: turn one finding into one edit of the localized master.

Design spec section 10. Four methods, ordered by cost and by risk of looking
fake, and the spine proves three of them on real pixels and real audio:

    relettering  the on-screen text is re-lettered in the market's language
    prop_swap    the offending prop becomes a market-appropriate one
    revoice      the offending line is re-spoken, compliant, over the same span
    reframe      the frame punches in for the span, regenerating no pixels

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
import re
import threading
import uuid
import wave
from pathlib import Path

from google.genai import types

from customs import costs, media
from customs.config import settings
from customs.genai_client import client, generate_bridge, generate_json
from customs.media import Shot
from customs.packs import load as load_packs
from customs.schema import ChangeRecord, Finding, Observation

class RemediationBlocked(Exception):
    """Raised when a finding must never be auto-remediated (the guard's call)."""

class RemediationError(Exception):
    """Raised when an edit could not be produced (no image back, no audio back)."""

# "bridge" is never chosen by plan(): it regenerates pixels and costs real
# money, so it only ever runs because an operator picked it in the console
# and the day's budget allowed it.
METHODS = ("relettering", "prop_swap", "revoice", "reframe", "bridge")

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
# everything else                reframe      no way to translate or swap it, so
#                                             exclude it from frame instead
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
DEFAULT_METHOD = "reframe"

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
    "reframe": None,
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

_DEFAULT_REPLACEMENT = {
    "relettering": "the same sentence translated into the language of {market_name}",
    "prop_swap": (
        "Replace each alcoholic drink, bottle and glass that is already "
        "visible with a non-alcoholic drink that suits {market_name}, for "
        "example tea in the same style of glass, keeping the same number of "
        "items in the same places and adding none."
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
    edited frame as an inline_data part. Any text part the model also returns
    is ignored; only pixels matter here.

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
    response = client().models.generate_content(
        model=settings.model_image,
        contents=parts,
        config=types.GenerateContentConfig(response_modalities=["TEXT", "IMAGE"]),
    )
    for candidate in (response.candidates or []):
        for part in (getattr(candidate.content, "parts", None) or []):
            inline = getattr(part, "inline_data", None)
            if inline and inline.data and (inline.mime_type or "").startswith("image/"):
                return inline.data
    raise RemediationError(
        f"{settings.model_image} returned no image part for this edit"
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

def _edit_frame_onto(base: Path, finding: Finding, method: str, replacement: str | None,
                     before: Path, workdir: Path, out_path: Path,
                     intent: str | None = None) -> tuple[Path, str]:
    """relettering / prop_swap: edit the keyframe, fit it, composite the span."""
    market_name = _market_name(finding.market)
    directive = _directive_for(finding, intent)
    if directive:
        # an intent with its own directive replaces the whole instruction
        instruction = (f"Edit this frame from a television commercial. {directive} "
                       f"Keep the lighting, the composition, the camera angle and "
                       f"every other pixel of the frame identical. Add nothing "
                       f"that is not already in the frame. The market is "
                       f"{market_name}.")
        edited_raw = workdir / f"{before.stem}_edited_raw.png"
        edited_raw.parent.mkdir(parents=True, exist_ok=True)
        edited_raw.write_bytes(_edit_image(instruction, before.read_bytes()))
        edited = media.fit_image(edited_raw, base,
                                 before.with_name(f"{before.stem}_edited.png"))
        media.overlay_image(base, edited, finding.t_start, finding.t_end, out_path)
        return edited, instruction
    if replacement is None:
        subject = _DEFAULT_REPLACEMENT[method].format(market_name=market_name)
    elif method == "relettering":
        subject = f'the text "{replacement}"'
    else:
        subject = replacement
    instruction = _EDIT_INSTRUCTIONS[method].format(replacement=subject)
    if method == "prop_swap" and market_name not in instruction:
        instruction = f"{instruction} The market is {market_name}."

    edited_raw = workdir / f"{before.stem}_edited_raw.png"
    edited_raw.parent.mkdir(parents=True, exist_ok=True)
    edited_raw.write_bytes(_edit_image(instruction, before.read_bytes()))
    # the model picks its own output resolution; the composite needs the
    # master's exact pixel size or the still would cover only part of it.
    edited = media.fit_image(edited_raw, base, before.with_name(f"{before.stem}_edited.png"))

    media.overlay_image(base, edited, finding.t_start, finding.t_end, out_path)
    return edited, instruction

def _bridge_span(base: Path, finding: Finding, replacement: str | None,
                 workdir: Path, out_path: Path,
                 keep_dir: Path | None = None) -> tuple[Path, str]:
    """Edit both ends of the span and let Veo generate the motion between.

    The only method that can follow genuine 3D motion, and the only one that
    puts pixels on screen the brand never shot, so it is the expensive tier
    and the console prices it before anyone presses the button. Both anchors
    are edited with the same instruction the patch methods use, so the
    generated motion starts and ends on frames that are already compliant.
    """
    market_name = _market_name(finding.market)
    subject = replacement or _DEFAULT_REPLACEMENT["prop_swap"].format(market_name=market_name)
    instruction = _EDIT_INSTRUCTIONS["prop_swap"].format(replacement=subject)

    # The tail is edited against the head's result, not on its own. Two
    # independent edits answer the same question twice and rarely identically,
    # and Veo spends the span morphing one answer into the other -- which
    # looks exactly like the artefact it is. Chained, both ends hold the same
    # object and Veo only has to move it.
    anchors = []
    first_edit: bytes | None = None
    for tag, when in (("head", finding.t_start), ("tail", max(finding.t_start, finding.t_end - 0.08))):
        span = Shot(shot_id=f"bridge_{tag}", t_start=when, t_end=when + 0.04)
        raw = media.extract_keyframes(base, span, workdir / "bridge", per_shot=1)[0]
        edited_bytes = _edit_image(instruction, raw.read_bytes(), reference=first_edit)
        if first_edit is None:
            first_edit = edited_bytes
        edited_raw = workdir / f"bridge_{tag}_edited_raw.png"
        edited_raw.write_bytes(edited_bytes)
        anchors.append(media.fit_image(edited_raw, base, workdir / f"bridge_{tag}.png"))

    seconds = costs.bridge_seconds(finding.t_end - finding.t_start)
    # _BRIDGE_PROMPT, not `instruction`: the anchors already carry the swap,
    # and telling a video model to "replace every alcoholic drink with tea"
    # makes it add tea to the scene rather than leave the corrected frames
    # alone. See _BRIDGE_PROMPT.
    clip = generate_bridge(
        prompt=_BRIDGE_PROMPT,
        first_frame=anchors[0], last_frame=anchors[1],
        seconds=seconds, out_path=workdir / "bridge.mp4")
    # Keep the generated footage itself, not just the master it went into.
    # The scratch workdir is deliberately not mirrored, so a clip left there
    # is gone the next time the container is replaced, and "nothing is edited
    # silently" has to cover the seconds a model invented most of all: they
    # are the ones a brand will want to watch before signing anything.
    if keep_dir is not None:
        keep_dir.mkdir(parents=True, exist_ok=True)
        kept = keep_dir / f"{out_path.stem.split('_')[0]}_bridge.mp4"
        try:
            kept.write_bytes(Path(clip).read_bytes())
        except OSError:
            pass
    media.splice_clip(base, clip, finding.t_start, finding.t_end, out_path)
    return anchors[0], instruction


def apply(run, finding: Finding, method: str, workdir, store, *,
          replacement: str | None = None, intent: str | None = None) -> ChangeRecord:
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
        return _apply_locked(run, finding, method, workdir, store, intent, replacement, changes_dir)

def _apply_locked(run, finding: Finding, method: str, workdir: Path, store,
                  intent: str | None,
                  replacement: str | None, changes_dir: Path) -> ChangeRecord:
    """apply()'s body, with this market's master already locked."""
    master = localized_master(run, finding.market, store)
    base = master if master.exists() else Path(run.asset_path)
    change_id = f"chg_{uuid.uuid4().hex[:12]}"
    store.emit(run.id, "remediator",
               f"{method} -> {finding.id} ({finding.rule_id}, {finding.market}) "
               f"on {base.name} at {finding.t_start:.2f}-{finding.t_end:.2f}s")
    store.update_finding_status(finding.id, "remediating", run_id=run.id)

    before = _still(base, change_id, "before", finding, changes_dir)
    staged = run_dir(run, store) / f".{change_id}_{master.name}"

    try:
        edit = _run_method(run, finding, method, replacement, intent, base, before,
                           workdir, staged, store, changes_dir)
    except Exception:
        # A half-done remediation must never look like a done one. The finding
        # goes straight back to open, so clearance() counts it again and the
        # alert stays up: design spec section 14, "remediation failure leaves
        # the original media untouched and the alert unresolved". The master is
        # untouched by construction -- every method writes to `staged` and only
        # the line after this block replaces the master with it.
        store.update_finding_status(finding.id, "open", run_id=run.id)
        staged.unlink(missing_ok=True)
        store.emit(run.id, "remediator",
                   f"stage_error: remediate: {method} failed for {finding.id}; "
                   f"{master.name} untouched, finding back to open")
        raise

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
                changes_dir: Path | None = None) -> str:
    """Produce the edited video at `staged` and return the change description.

    Split out of apply() so every failure mode of every method funnels through
    one try/except there, rather than each method having to remember to put
    the finding back.
    """
    market_name = _market_name(finding.market)
    if method in ("relettering", "prop_swap"):
        _edited, instruction = _edit_frame_onto(
            base, finding, method, replacement, before, workdir, staged,
            intent=intent)
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
    if method == "bridge":
        _edited, instruction = _bridge_span(base, finding, replacement,
                                            Path(workdir), staged,
                                            keep_dir=changes_dir)
        return (f"regenerated {costs.bridge_seconds(finding.t_end - finding.t_start):.0f}s "
                f"of motion between two edited anchor frames")
    media.crop_span(base, finding.t_start, finding.t_end, staged)
    return (
        "centre crop for the span, excluding the outer edge of the frame "
        "without regenerating any pixels"
    )
