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
statement naming only what is visible or audible, and your confidence 0..1.
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
            "confidence": {"type": "number"},
        },
        "required": ["dimension", "statement", "confidence"],
    },
}

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

def observe_shot(video_path, shot: Shot, workdir, on_event=None) -> list[Observation]:
    """Run the neutral observation pass on one shot, return its Observations.

    Extracts keyframes, calls the vision model once with the prompt (taxonomy
    interpolated), the keyframes, the transcript span placeholder and the
    shot timecodes, and turns each returned item into an Observation. Items
    whose dimension is not in the taxonomy are dropped with a warning event
    rather than raising, so one bad item never loses the whole shot.
    """
    _emit(on_event, f"observe -> {shot.shot_id}")

    keyframes = extract_keyframes(video_path, shot, Path(workdir) / "frames")

    dims = taxonomy()
    prompt_text = PROMPT.format(taxonomy=sorted(dims))
    parts = [prompt_text]
    for kf in keyframes:
        parts.append(types.Part.from_bytes(data=kf.read_bytes(), mime_type="image/jpeg"))
    parts.append(f"Transcript span: {TRANSCRIPT_UNAVAILABLE}")
    parts.append(f"Shot timecodes: t_start={shot.t_start:.3f}s, t_end={shot.t_end:.3f}s")

    raw = generate_json(settings.model_vision, parts, _RESPONSE_SCHEMA)

    evidence_frame = str(keyframes[0]) if keyframes else ""
    observations = []
    for i, item in enumerate(raw):
        dimension = item.get("dimension")
        if dimension not in dims:
            _emit(
                on_event,
                f"warning: dropped observation in {shot.shot_id}, "
                f"unknown dimension {dimension!r}",
            )
            continue
        observations.append(Observation(
            id=f"obs_{shot.shot_id}_{i:03d}",
            shot_id=shot.shot_id,
            t_start=shot.t_start,
            t_end=shot.t_end,
            dimension=dimension,
            statement=str(item.get("statement", "")),
            evidence_frame=evidence_frame,
            confidence=float(item.get("confidence", 0.0)),
        ))
    return observations

def observe_all(video_path, workdir, on_event=None) -> list[Observation]:
    """Run observe_shot sequentially over every (merged) shot in the video.

    Shots shorter than MIN_SHOT_LEN are merged into the previous shot first
    (see merge_micro_shots), since our scene-cut detector fragments things
    like strobe transitions into many sub-second segments that are not worth
    a separate model call each.
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
        observations.extend(observe_shot(video_path, shot, workdir, on_event=on_event))
    return observations
