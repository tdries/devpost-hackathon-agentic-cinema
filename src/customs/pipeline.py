import time
from pathlib import Path

from google.genai import types

from customs.adjudicate import clearance, judge
from customs.analyst import merge_micro_shots, observe_shot
from customs.config import settings
from customs.genai_client import generate_json
from customs.media import Shot, detect_shots, extract_audio_span
from customs.packs import load as load_packs
from customs.schema import Finding, Observation, RunRecord
from customs.store import Store

# Model-call retries live here, at the pipeline level, never inside
# analyst.py or adjudicate.py (design spec section 14): "Model call failure:
# retry with backoff, three attempts, then record a stage error." Applied to
# every model-calling unit: one shot's transcription, one shot's analyst
# observation, one market's adjudication.
_MAX_ATTEMPTS = 3
_BACKOFF_BASE_SECONDS = 1.0

def _call_with_retries(fn):
    """Call fn() up to _MAX_ATTEMPTS times with exponential backoff.

    Returns (True, value) on the first successful attempt. Returns (False,
    last_exception) once every attempt has raised -- the caller is
    responsible for recording the stage_error and skipping that unit; this
    helper only knows how to retry, not what unit or stage it is retrying.
    """
    last_exc: Exception | None = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            return True, fn()
        except Exception as exc:  # noqa: BLE001 -- any model/ffmpeg failure here is retryable
            last_exc = exc
            if attempt < _MAX_ATTEMPTS - 1:
                time.sleep(_BACKOFF_BASE_SECONDS * (2 ** attempt))
    return False, last_exc

# Transcription prompt: not specified verbatim by the brief (unlike
# analyst.PROMPT / adjudicate.PROMPT), just its intent -- "asking for a
# verbatim transcript, empty string if no speech".
TRANSCRIBE_PROMPT = (
    "Transcribe verbatim any speech audible in this audio clip. Return only "
    "the spoken words, exactly as said, with no descriptions or added "
    "commentary. If there is no speech at all, return an empty string."
)

_TRANSCRIPT_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {"transcript": {"type": "string"}},
    "required": ["transcript"],
}

def _transcribe_shot(video_path, shot: Shot, workdir) -> str:
    """Extract one shot's audio span and transcribe it verbatim via Gemini.

    Returns an empty string both when the model reports no speech and when
    its response is malformed (mirrors the shape-hygiene style established
    in analyst.py/adjudicate.py: never raise on a malformed shape). The
    ffmpeg extraction and the model call itself can still raise; that is
    exactly what the retry wrapper around this whole function is for.
    """
    wav_path = extract_audio_span(video_path, shot, Path(workdir) / "audio")
    audio_part = types.Part.from_bytes(data=wav_path.read_bytes(), mime_type="audio/wav")
    raw = generate_json(settings.model_text, [TRANSCRIBE_PROMPT, audio_part], _TRANSCRIPT_RESPONSE_SCHEMA)
    if not isinstance(raw, dict):
        return ""
    text = raw.get("transcript")
    return text if isinstance(text, str) else ""

def apply_guard(findings: list[Finding]) -> list[Finding]:
    """Identity placeholder for the guard stage (design spec section 7).

    Task 10 replaces this body with the real protected-characteristic
    blocking rule layer (guard.apply); kept under this exact name, called
    from exactly one place below (with `pack` already in scope there), so
    that swap is a body change plus a one-line call-site change, not a
    redesign.
    """
    return findings

def run(asset_path, markets: list[str], store: Store, workdir) -> RunRecord:
    """Run the full clearance pipeline for one asset across the given markets.

    Sequential stages: create the run, ingest (shot detection + merge, then
    per-shot transcription), analyst (per-shot observation), adjudicate
    (per-market judging + guard placeholder), persist, done.

    Every stage that makes a model call -- one shot's transcription, one
    shot's analyst observation, one market's adjudication -- is wrapped in
    _call_with_retries. On final failure the unit is skipped: a
    "stage_error: ..." event is recorded via store.emit and the run
    continues (design spec section 14, "a clearance tool that silently
    skips a shot is worse than one that admits it"). The run always ends in
    status "done"; failures are visible as stage_error events, never as a
    different terminal status or a raised exception out of this function.

    Shots are iterated directly here (media.detect_shots + merge_micro_shots)
    rather than via analyst.observe_all, precisely so each shot's model call
    can be retried and, on final failure, skipped independently -- observe_all
    calls observe_shot in a plain loop with no per-shot fault isolation, and
    per-task-9-brief ruling retries must live at the pipeline level, not
    inside analyst.py. merge_micro_shots is therefore called exactly once,
    here; observe_all's own internal merge is simply never invoked.
    """
    workdir = Path(workdir)
    asset_path = str(asset_path)
    run_record = store.create_run(asset_path=asset_path, markets=list(markets))
    run_id = run_record.id

    def emit(agent: str, message: str) -> None:
        store.emit(run_id, agent, message)

    store.set_run_t0(run_id, time.time())
    store.set_run_status(run_id, "running")
    emit("pipeline", f"run {run_id} started: asset={asset_path} markets={list(markets)}")

    # --- ingest: shot detection + micro-shot merge (once, not per-unit) ---
    emit("pipeline", "ingest -> detecting shots")
    raw_shots = detect_shots(asset_path)
    shots = merge_micro_shots(raw_shots)
    emit("pipeline", f"ingest -> {len(raw_shots)} raw shot(s) merged to {len(shots)}")

    # --- ingest: per-shot transcription (stage-wrapped, one unit per shot) ---
    transcripts: dict[str, str] = {}
    for shot in shots:
        ok, result = _call_with_retries(lambda shot=shot: _transcribe_shot(asset_path, shot, workdir))
        if ok:
            transcripts[shot.shot_id] = result
            emit("transcription", f"{shot.shot_id} -> {len(result)} char(s)")
        else:
            emit("transcription", f"stage_error: {shot.shot_id}: {result!r}")

    # --- analyst: per-shot observation (stage-wrapped, one unit per shot) ---
    observations: list[Observation] = []
    for shot in shots:
        ok, result = _call_with_retries(
            lambda shot=shot: observe_shot(asset_path, shot, workdir, on_event=emit, transcripts=transcripts)
        )
        if ok:
            observations.extend(result)
        else:
            emit("analyst", f"stage_error: {shot.shot_id}: {result!r}")
    store.add_observations(run_id, observations)
    emit("pipeline", f"analyst -> {len(observations)} observation(s) persisted")

    # --- adjudicate: per-market judging, sequential, plus guard placeholder
    # (stage-wrapped, one unit per market) ---
    packs = load_packs()
    all_findings: list[Finding] = []
    for market in markets:
        pack = packs.get(market)
        if pack is None:
            emit("adjudicator", f"stage_error: {market}: no market pack loaded")
            continue

        ok, result = _call_with_retries(lambda pack=pack: judge(run_id, observations, pack, on_event=emit))
        if not ok:
            emit("adjudicator", f"stage_error: {market}: {result!r}")
            continue

        findings = apply_guard(result)
        all_findings.extend(findings)
        status = clearance(findings)
        emit("adjudicator", f"{market} clearance -> {status} ({len(findings)} finding(s))")

    store.add_findings(all_findings)
    emit("pipeline", f"adjudicate -> {len(all_findings)} finding(s) persisted across {len(markets)} market(s)")

    store.set_run_status(run_id, "done")
    emit("pipeline", f"run {run_id} done")
    return store.get_run(run_id)
