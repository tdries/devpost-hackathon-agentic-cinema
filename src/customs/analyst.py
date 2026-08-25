from pathlib import Path

from google.genai import types

from customs.config import settings
from customs.genai_client import generate_json
from customs.media import Shot, detect_shots, extract_keyframes
from customs.packs import taxonomy
from customs.schema import Observation

# Shots shorter than this are fragments of a single visual event (e.g. a
# strobe transition), not a distinct scene worth its own model call. See the
# controller ruling in .superpowers/sdd/2026-08-23-customs/task-7-brief.md.
MIN_SHOT_LEN = 0.5

# Audio transcription is not part of this task; it arrives with the
# ingest/pipeline work. The prompt's transcript slot gets this literal text
# until then.
TRANSCRIPT_UNAVAILABLE = "(transcript not available)"

# Verbatim prompt text, single source of truth (task-7-brief.md Step 3).
# {taxonomy} is interpolated at call time via PROMPT.format(taxonomy=...)
# on a sorted list of the taxonomy dimensions; the rest of the text is exact.
PROMPT = """You are a neutral shot logger for advertising compliance. You are given
keyframes and the transcript span of one shot of a commercial. Record every
observable element that could conceivably matter to any culture, regulator,
or broadcaster on earth. Do not judge, do not localize, do not filter.
For each element emit: dimension (one of {taxonomy}), a one-sentence factual
statement naming only what is visible or audible, your confidence 0..1, and
frame_index: which supplied keyframe you saw it in, numbered from 0 in the
order given (use the earliest frame that shows it; for something heard and
not seen, the frame nearest when it is said), and box_2d: the bounding box
of the thing itself in THAT keyframe as [ymin, xmin, ymax, xmax] normalised
to 0-1000. Box the object, garment, text or gesture being described, tightly
-- not the whole person and not the whole frame. For something heard and not
seen, box the speaker if they are visible, otherwise return [0, 0, 0, 0].
Log products, drinks, food, clothing and skin exposure, gestures, symbols,
flags, religious items, text visible on screen (quote it exactly), claims
made in speech (quote them), humor devices, physical contact between people,
who is present, and anything flashing or strobing. Statements must be
verifiable from the pixels or audio alone."""

_RESPONSE_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "dimension": {"type": "string"},
            "statement": {"type": "string"},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "frame_index": {"type": "integer", "minimum": 0},
            "box_2d": {
                "type": "array", "minItems": 4, "maxItems": 4,
                "items": {"type": "integer", "minimum": 0, "maximum": 1000},
            },
        },
        "required": ["dimension", "statement", "confidence", "frame_index",
                     "box_2d"],
    },
}

_LOCATE_SCHEMA = {
    "type": "object",
    "properties": {
        "box_2d": {
            "type": "array", "minItems": 4, "maxItems": 4,
            "items": {"type": "integer", "minimum": 0, "maximum": 1000},
        },
        "found": {"type": "boolean"},
    },
    "required": ["box_2d", "found"],
}

_LOCATE_PROMPT = (
    "This is one frame of a television commercial. A compliance analyst "
    "wrote this about it:\n\n  {statement}\n\n"
    "Return box_2d: the bounding box of the thing that sentence is about, "
    "as [ymin, xmin, ymax, xmax] normalised to 0-1000. Box the object, "
    "garment, text or gesture itself as tightly as you can -- not the whole "
    "person, not the whole frame. found: false if the sentence is about "
    "something audible, or about the scene as a whole, or you cannot see it "
    "in this frame; box_2d must then be [0, 0, 0, 0]."
)


class LocateFailed(RuntimeError):
    """The call did not happen. Distinct from "there is nothing to box".

    Collapsing the two is how an expired credential looked exactly like a
    frame with nothing in it -- and worse, would have been written back to
    the store as a permanent empty box, so the observation could never be
    located again even once the credential was fixed.
    """


def locate(image_bytes: bytes, statement: str) -> list:
    """Where in this frame is the thing that sentence describes?

    For observations recorded before the analyst was asked for a box. One
    image call, computed once and written back to the store. Returns an
    empty list when there is genuinely nothing to box, and RAISES when the
    call failed: no rectangle is better than a wrong one drawn confidently
    over someone's commercial, but a failure must not be cached as an
    answer.
    """
    from customs.genai_client import generate_json_image
    try:
        answer = generate_json_image(
            _LOCATE_PROMPT.format(statement=statement or "(no statement)"),
            image_bytes, _LOCATE_SCHEMA)
    except Exception as exc:  # noqa: BLE001 -- surfaced, not swallowed
        raise LocateFailed(str(exc)[:200]) from exc
    if not answer.get("found"):
        return []
    raw = answer.get("box_2d")
    if not isinstance(raw, list) or len(raw) != 4:
        return []
    try:
        ymin, xmin, ymax, xmax = (max(0, min(1000, int(v))) for v in raw)
    except (TypeError, ValueError):
        return []
    return [ymin, xmin, ymax, xmax] if ymax > ymin and xmax > xmin else []


def merge_micro_shots(shots: list[Shot], min_len: float = MIN_SHOT_LEN) -> list[Shot]:
    """Fold any shot shorter than min_len seconds into the previous shot.

    Pure function: returns new Shot instances, never mutates the input list
    or its elements. A folded-in shot only extends the previous shot's
    t_end; the previous shot keeps its own shot_id and t_start.

    The first shot has no previous shot to fold into. If it is itself too
    short, it instead absorbs forward: it keeps its own shot_id and t_start
    and extends its t_end by folding in the shot(s) after it, repeating
    until it clears min_len or nothing is left to absorb.
    """
    if not shots:
        return []

    merged = [Shot(shots[0].shot_id, shots[0].t_start, shots[0].t_end)]
    for shot in shots[1:]:
        if shot.t_end - shot.t_start < min_len:
            prev = merged[-1]
            merged[-1] = Shot(prev.shot_id, prev.t_start, shot.t_end)
        else:
            merged.append(Shot(shot.shot_id, shot.t_start, shot.t_end))

    while len(merged) > 1 and merged[0].t_end - merged[0].t_start < min_len:
        first, second = merged[0], merged[1]
        merged[0] = Shot(first.shot_id, first.t_start, second.t_end)
        del merged[1]

    return merged

def _emit(on_event, message: str) -> None:
    if on_event is not None:
        on_event("analyst", message)

def observe_shot(video_path, shot: Shot, workdir, on_event=None, transcripts=None) -> list[Observation]:
    """Run the neutral observation pass on one shot, return its Observations.

    Extracts keyframes, calls the vision model once with the prompt (taxonomy
    interpolated), the keyframes, the transcript span text and the shot
    timecodes, and turns each returned item into an Observation. A malformed
    response never crashes the pass, it only shrinks it: a non-list response
    drops the whole shot with one warning event; a non-dict item, an unknown
    dimension, or an empty statement (including an explicit JSON null) drops
    just that item with its own warning event.

    transcripts is an optional dict of shot_id -> verbatim transcript text
    (task-9's pipeline produces it, one Gemini audio call per shot, before
    calling this function). Default None keeps Task 7's original behavior
    exactly: every shot gets the TRANSCRIPT_UNAVAILABLE placeholder. When a
    dict is given, this shot's own text is looked up by shot_id; a shot
    missing from the dict (its own transcription stage errored and was
    skipped upstream) falls back to the same placeholder rather than
    KeyError-ing.
    """
    _emit(on_event, f"observe -> {shot.shot_id}")

    keyframes = extract_keyframes(video_path, shot, Path(workdir) / "frames")

    dims = taxonomy()
    prompt_text = PROMPT.format(taxonomy=sorted(dims))
    parts = [prompt_text]
    for kf in keyframes:
        parts.append(types.Part.from_bytes(data=kf.read_bytes(), mime_type="image/jpeg"))
    transcript_text = TRANSCRIPT_UNAVAILABLE if transcripts is None else transcripts.get(shot.shot_id, TRANSCRIPT_UNAVAILABLE)
    parts.append(f"Transcript span: {transcript_text}")
    parts.append(f"Shot timecodes: t_start={shot.t_start:.3f}s, t_end={shot.t_end:.3f}s")

    raw = generate_json(settings.model_vision, parts, _RESPONSE_SCHEMA)

    if not isinstance(raw, list):
        _emit(
            on_event,
            f"warning: dropped whole response for {shot.shot_id}, "
            f"expected a list, got {type(raw).__name__}",
        )
        return []

    # Which frame each observation points at. The analyst now looks at up to
    # eight frames of a long take, so recording the shot's first frame for
    # all of them put a thumbnail from 00:00 next to a finding at 00:30. The
    # model names the frame it saw; anything missing or out of range falls
    # back to the first, which is what this always used to do.
    def _box_of(item: dict) -> list:
        """The four numbers, or nothing rather than a wrong rectangle.

        Drawn over the frame in the browser, never into it: the PNG is what
        a remediation edits and what Veo is anchored on, so a rectangle
        burned into it would end up in the commercial.
        """
        raw = item.get("box_2d")
        if not isinstance(raw, list) or len(raw) != 4:
            return []
        try:
            box = [max(0, min(1000, int(v))) for v in raw]
        except (TypeError, ValueError):
            return []
        ymin, xmin, ymax, xmax = box
        # a zero-area or inverted box is a refusal, not a location
        if ymax <= ymin or xmax <= xmin:
            return []
        return box

    def _evidence_for(item: dict) -> str:
        if not keyframes:
            return ""
        index = item.get("frame_index")
        if isinstance(index, bool) or not isinstance(index, int):
            return str(keyframes[0])
        return str(keyframes[index]) if 0 <= index < len(keyframes) else str(keyframes[0])

    observations = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            _emit(
                on_event,
                f"warning: dropped observation {i} in {shot.shot_id}, "
                f"expected an object, got {type(item).__name__}",
            )
            continue

        dimension = item.get("dimension")
        if dimension not in dims:
            _emit(
                on_event,
                f"warning: dropped observation in {shot.shot_id}, "
                f"unknown dimension {dimension!r}",
            )
            continue

        # JSON null survives the response_schema's "string" type, so an
        # explicit null must be coalesced by hand, not just defaulted via
        # dict.get's fallback (which only applies when the key is absent).
        statement = item.get("statement")
        if not isinstance(statement, str):
            statement = ""
        if not statement:
            _emit(
                on_event,
                f"warning: dropped observation in {shot.shot_id}, empty statement",
            )
            continue

        confidence = item.get("confidence")
        if confidence is None:
            confidence = 0.0
        confidence = max(0.0, min(1.0, float(confidence)))

        observations.append(Observation(
            id=f"obs_{shot.shot_id}_{i:03d}",
            shot_id=shot.shot_id,
            t_start=shot.t_start,
            t_end=shot.t_end,
            dimension=dimension,
            statement=statement,
            evidence_frame=_evidence_for(item),
            confidence=confidence,
            box=_box_of(item),
        ))
    return observations

def observe_all(video_path, workdir, on_event=None, transcripts=None) -> list[Observation]:
    """Run observe_shot sequentially over every (merged) shot in the video.

    Shots shorter than MIN_SHOT_LEN are merged into the previous shot first
    (see merge_micro_shots), since our scene-cut detector fragments things
    like strobe transitions into many sub-second segments that are not worth
    a separate model call each.

    transcripts (optional dict of shot_id -> text, default None) is passed
    through unchanged to every observe_shot call; each call does its own
    per-shot lookup by shot_id, so this function does no lookup of its own.
    """
    raw_shots = detect_shots(video_path)
    shots = merge_micro_shots(raw_shots, min_len=MIN_SHOT_LEN)
    n_merged = len(raw_shots) - len(shots)
    _emit(
        on_event,
        f"merged {n_merged} micro-segment(s) shorter than {MIN_SHOT_LEN}s "
        f"({len(raw_shots)} shots -> {len(shots)})",
    )

    observations = []
    for shot in shots:
        observations.extend(observe_shot(video_path, shot, workdir, on_event=on_event, transcripts=transcripts))
    return observations
