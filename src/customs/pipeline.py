import time
from pathlib import Path

from google.genai import types

from customs import guard
from customs.adjudicate import clearance, judge
from customs.analyst import merge_micro_shots, observe_shot
from customs.config import settings
from customs.genai_client import generate_json
from customs.media import Shot, detect_flashes, detect_shots, extract_audio_span
from customs.packs import MarketPack, load as load_packs
from customs.schema import Finding, Observation, RunRecord
from customs.store import Store

# These names are also the crew's seam. Since Task 13 the stages live in
# crew.py, and every one of them reaches its work through this module
# (pipeline.judge, pipeline.observe_shot, pipeline.detect_shots, ...) rather
# than importing analyst/adjudicate/media directly. That keeps one definition
# of "which function is a stage", and keeps this module the single place a
# caller or a test patches to steer a run.

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

# Design spec section 5 / the photosensitivity dimension: more than three
# full-frame flashes per second is the line every broadcaster's version of the
# Harding test draws. media.detect_flashes measures the rate; this is where the
# rate becomes a judgement, so the number lives here and not in the media
# toolkit.
FLASH_RATE_THRESHOLD = 3.0

def flash_observations(asset_path, shots: list[Shot]) -> list[Observation]:
    """Deterministic photosensitivity observations for one asset's ingest.

    The analyst reads still keyframes, so it is structurally blind to
    flashing: the Task 9 gate missed test_ad.mp4's planted 6 flashes/second
    strobe for that reason, and no prompt change fixes it because the
    evidence is not in any single frame. media.detect_flashes measures the
    rate off the pixels instead, and every window over FLASH_RATE_THRESHOLD
    becomes an Observation in the same shape the analyst emits, so the
    adjudicator judges it against the market packs' photosensitivity rules
    with no special case anywhere downstream.

    confidence is 1.0 because this is a measurement, not an opinion. The
    observation is attributed to the shot its window starts in (so the
    Verifier can re-observe that shot), or to a synthetic id when no shot
    contains it.
    """
    observations = []
    for i, window in enumerate(detect_flashes(asset_path)):
        if window.flashes_per_second <= FLASH_RATE_THRESHOLD:
            continue
        shot = next(
            (s for s in shots if s.t_start <= window.t_start < s.t_end), None
        )
        observations.append(Observation(
            id=f"obs_flash_{i:03d}",
            shot_id=shot.shot_id if shot else f"flash_{i}",
            t_start=window.t_start,
            t_end=window.t_end,
            dimension="photosensitivity_sensory",
            statement=(
                f"Full-frame luminance flashing measured at "
                f"{window.flashes_per_second:.1f} flashes per second between "
                f"{window.t_start:.2f}s and {window.t_end:.2f}s"
            ),
            evidence_frame="",
            confidence=1.0,
        ))
    return observations

def apply_guard(findings: list[Finding], pack: MarketPack) -> list[Finding]:
    """Guard stage: delegate to guard.apply (design spec section 7).

    Kept under this exact name, at the one call site below, per the Task 9
    placeholder's own contract -- the Task 10 swap is this body plus the
    added `pack` parameter (already in scope at the call site), not a
    redesign. All the actual rule logic (protected_basis blocking,
    offence-never-remediable) lives in guard.py, not here; see its
    docstring for why it is written as a pure function with no model call.
    """
    return guard.apply(findings, pack)

def errored_markets(store: Store, run_id: str) -> set[str]:
    """Every market that was never actually evaluated in this run.

    A market lands here when its pack failed to load, or its judge() call
    exhausted every retry (the two market-level failure branches in
    crew.AdjudicatorAgent) -- never for a per-shot transcription/analyst failure, which
    thins the evidence every market sees but does not mean any one market
    went unjudged.

    Derived from the run's own event log (agent="adjudicator" events whose
    message starts with "stage_error: market={code}:") rather than a new
    Store method or a new RunRecord field, so any caller -- this task's own
    CLI, or a future Task 11/15 consumer -- can ask "was this market really
    evaluated" from the one existing source of truth instead of trusting
    clearance([]) == "cleared" for a market that was never judged at all.
    """
    errored: set[str] = set()
    prefix = "stage_error: market="
    for _id, _ts, agent, message in store.events_since(run_id, 0):
        if agent == "adjudicator" and message.startswith(prefix):
            errored.add(message[len(prefix):].split(":", 1)[0])
    return errored

def run(asset_path, markets: list[str], store: Store, workdir) -> RunRecord:
    """Run the full clearance pipeline for one asset across the given markets.

    Stages: create the run, ingest (shot detection + merge, then per-shot
    transcription), analyst (per-shot observation), adjudicate (per-market
    judging, now in parallel), guard, persist, publish, done.

    Since Task 13 the stages are an ADK agent graph and this function is a
    thin, stable front door onto it: it delegates to crew.run_clearance and
    keeps its own signature, its stage-error semantics and its CLI contract
    unchanged. Everything below still describes what a run does, because the
    crew's agents call these same functions -- _transcribe_shot,
    observe_shot, judge, apply_guard -- through this module, which is also
    why monkeypatching pipeline.judge still steers a run.

    The one stage the crew adds is the Publisher (crew.PublisherAgent), which
    pushes this run's telemetry and updates the Grafana dashboards over MCP.
    It cannot fail a run: a dead Grafana records `stage_error: publisher` and
    the run still completes with its findings persisted.

    Every stage that makes a model call -- one shot's transcription, one
    shot's analyst observation, one market's adjudication -- is wrapped in
    _call_with_retries. On final failure the unit is skipped: a
    "stage_error: ..." event is recorded via store.emit and the run
    continues (design spec section 14, "a clearance tool that silently
    skips a shot is worse than one that admits it"). For those three
    retry-wrapped stages specifically, failure never raises out of this
    function and never changes the terminal status: it surfaces only as a
    stage_error event, and the run still reaches status "done". That
    guarantee does NOT extend to shot detection itself (media.detect_shots,
    called once by the ingest stage before any per-shot work): an unreadable or corrupt
    asset raises there, uncaught, leaving the run in status "running"
    rather than "done" -- there is no reasonable per-unit failure to skip
    when there are no shots at all to iterate.

    A market whose pack failed to load or whose judge() call exhausted
    every retry is recorded, not silently reported as clean: see
    errored_markets() above, which callers use to distinguish "evaluated,
    found nothing" from "never evaluated" rather than trusting
    clearance([]) == "cleared" for both.

    Shots are iterated one at a time (media.detect_shots + merge_micro_shots
    in ingest, then observe_shot per shot in the analyst stage) rather than
    via analyst.observe_all, precisely so each shot's model call
    can be retried and, on final failure, skipped independently -- observe_all
    calls observe_shot in a plain loop with no per-shot fault isolation, and
    per-task-9-brief ruling retries must live at the pipeline level, not
    inside analyst.py. merge_micro_shots is therefore called exactly once,
    in ingest; observe_all's own internal merge is simply never invoked.
    """
    from customs import crew  # local import: crew.py imports this module

    return crew.run_clearance(asset_path, markets, store, workdir)


def judge_more(store: Store, run: RunRecord, markets: list[str],
               duration: float, on_event=None) -> dict[str, str]:
    """Clear an already-observed asset against markets it was not run for.

    The expensive half of a run is market-independent. Shot detection,
    keyframe extraction, transcription and the analyst's observations
    describe what is on screen, in a fixed taxonomy (packs.taxonomy) that no
    market chooses, and adjudicate.candidates() joins those observations to
    a market's rules purely on dimension. So a second market does not need
    the film opened again: it needs its own rules held against the same
    observations, which is one judge() call per market and nothing else.

    That is the whole point of observing once and judging many times, and
    until now the console had no way to ask for it -- a Belgian clearance
    followed by a French one meant downloading, cutting and re-observing an
    asset the store had already described.

    Findings are appended, never replacing what other markets found. Grafana
    gets the new markets' status and log lines, plus their risk samples on
    the run's existing mapped clock via telemetry.extend_timeline: t0 is
    already fixed and re-picking it would strand every sample the first pass
    wrote.
    """
    from customs import telemetry

    def emit(agent: str, message: str) -> None:
        store.emit(run.id, agent, message)
        if on_event is not None:
            on_event(agent, message)

    observations = store.observations(run.id)
    if not observations:
        raise ValueError(f"run {run.id} has no observations to judge against")

    packs = load_packs()
    emit("pipeline", f"add analysis -> {len(observations)} stored observation(s), "
                     f"no re-ingest, judging {', '.join(markets)}")

    fresh: list[Finding] = []
    clearances: dict[str, str] = {}
    for market in markets:
        pack = packs.get(market)
        if pack is None:
            emit("adjudicator", f"stage_error: market={market}: no market pack loaded")
            continue
        ok, judged = _call_with_retries(
            lambda: judge(run.id, observations, pack, on_event=emit))
        if not ok:
            emit("adjudicator", f"stage_error: market={market}: {judged!r}")
            continue
        findings = apply_guard(judged, pack)
        status = clearance(findings)
        clearances[market] = status
        fresh.extend(findings)
        emit("adjudicator", f"{market} clearance -> {status} ({len(findings)} finding(s))")

    if fresh:
        store.add_findings(fresh)

    # Telemetry is best effort, exactly as it is for a first pass: a dead
    # Grafana must not lose findings that are already in SQLite.
    try:
        run = store.get_run(run.id)
        everything = store.findings(run.id)
        telemetry.extend_timeline(run, everything, duration, list(clearances))
        # the new market's verdicts change which observations are flagged,
        # so the observation lines are refreshed too
        telemetry.push_observations(run, store.observations(run.id), everything)
        for market, status in clearances.items():
            telemetry.push_status(run, market, status, everything)
        for finding in fresh:
            telemetry.push_log(run, finding)
    except Exception as exc:  # noqa: BLE001
        emit("publisher", f"stage_error: publisher: add analysis: {exc!r}")

    return clearances
