"""The Customs HTTP service: Launch Control, and the Grafana alert webhook.

Two surfaces, one process, no build step.

**Launch Control** (Task 15) is the console and the demo: upload an asset,
watch fifteen -- or three -- market tiles flip from pending to cleared, at
risk or blocked as the adjudicators return, read the agents' own log as it
happens, open one market and see the statute behind every finding, and watch
the original and the localized master play side by side in the Cutting Room.
Server-rendered Jinja2, one stylesheet, one small script. Nothing is built,
bundled or fetched from a CDN, because the thing a judge opens must not
depend on a toolchain being alive.

**The alert webhook** is the seam where Grafana stops being a report and
becomes the thing that wakes the agent (design spec section 9b: "Grafana is
upstream of the work, not a report produced afterwards"). It predates the
console and is deliberately untouched by it.

An alert rule fires, its contact point POSTs here, and this service
remediates the finding the alert is about and verifies the fix, which drops
the metric and lets Grafana resolve its own alert. Two rules govern that
route, and the console changed neither of them:

1. **Nothing in the payload body is trusted except the labels.** An alert is
   an unauthenticated external input. `{asset, market, rule_id}` are used as
   a lookup key into the run store and every other field -- severity, values,
   annotations, any "finding_id" someone puts in the body -- is ignored. The
   finding that gets remediated is the one the store says is open for those
   three labels, or none at all. A forged rule_id therefore does nothing at
   all, and a forged label set cannot point the Remediator at a finding the
   guard blocked, because the guard's decision is re-checked in
   remediate.apply anyway.
2. **Answer 200 immediately.** Remediation is minutes of ffmpeg and model
   calls; Grafana's webhook has a short timeout and retries what it thinks
   failed. The work runs as a FastAPI BackgroundTask after the response, and
   the response says only how many alerts were accepted.

--- Where the work runs ---

Three kinds of slow work hang off this file, and each gets the mechanism it
actually needs rather than the same one three times:

* **A clearance run is a thread.** `POST /runs` starts `crew.run_clearance`
  on a plain `threading.Thread`, not a FastAPI BackgroundTask. A run is
  minutes of ffmpeg and Vertex calls; a BackgroundTask runs *after* the
  response is finished but still inside the request's task, so the browser
  would sit on the POST for the whole run and `TestClient` would block on it
  until the run ended. A thread lets the POST answer immediately with the
  redirect the browser needs, which is the entire point of creating the run
  record in the request instead of in the crew.
* **A remediation is a BackgroundTask**, exactly as the webhook has always
  done it: seconds to a minute, and the caller (Grafana, or the Market Room's
  button) only needs to know it was accepted.
* **The mission feed is an async generator** polling the store off the event
  loop with `asyncio.to_thread`, because SSE holds the connection open for
  the length of the run and must never occupy a worker thread doing nothing.

--- Reading the run store ---

Every screen is derived from the store and nothing else. There is no console
state, no cache of "what the run is doing", no second source of truth: the
board asks `adjudicate.clearance` and `pipeline.errored_markets` the same
questions the CLI asks, and a market that was never judged is drawn as ERROR
rather than as a market with no findings. That is the one honesty rule this
console has to keep, because "cleared" and "never evaluated" look identical
to anything that only counts findings.
"""
import asyncio
import json
import logging
import re
import threading
import time
import uuid
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

from fastapi import (BackgroundTasks, FastAPI, File, Form, HTTPException,
                     Request, UploadFile)
from fastapi import Response
from fastapi.responses import (FileResponse, HTMLResponse, PlainTextResponse,
                               RedirectResponse, StreamingResponse)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from customs import (adjudicate, agentmode, analyst, costs, media, packs, replyfmt,
                     persist, pipeline, remediate, scope as scope_mod, spark,
                     state as state_mod, verify)
from customs.fetch import FetchError, fetch_youtube
from customs.config import settings
from customs.media import MediaError, probe_duration
from customs.store import Store

log = logging.getLogger("customs.app")

app = FastAPI(title="Customs Launch Control",
              description="Ad clearance crew: console, mission feed, alert webhook")

_HERE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(_HERE / "templates"))
app.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")

# Cache-buster for the two static assets, stamped into their URLs by
# base.html. Browsers heuristically cache /static without revalidating, so a
# deploy that changes the stylesheet would otherwise leave returning visitors
# clicking new buttons against old CSS (which is exactly what happened when
# the style modes shipped). The newest mtime of the two files changes on
# every image build that touches them, and that is the only time it needs to.
templates.env.globals["static_v"] = str(int(max(
    (_HERE / "static" / name).stat().st_mtime
    for name in ("customs.css", "customs.js"))))

# Scratch space for the frames and audio remediation extracts. Same default
# the CLI uses (scripts/run_pipeline.py --workdir). A clearance run gets its
# own runs/work/{run_id} under here (see _clearance_job): the ingest and
# analyst stages name their scratch frames and audio by shot_id, and
# media.detect_shots numbers every video's shots from 0, so shot_0 exists
# for every asset. Two runs sharing this directory directly would overwrite
# each other's frames mid-run and the Analyst could end up judging the wrong
# video. Remediation's own workdir (see remediate_and_verify) stays this
# shared root: its scratch files are named by change_id, a uuid4, which is
# already globally unique, so there is nothing there for two runs to collide
# on.
WORKDIR = Path("runs/work")

# Boot: pull the previous revision's runs back out of the mounted bucket
# before the store is ever opened, so the first request already sees them.
# Without CUSTOMS_STATE_DIR this is a no-op and the service behaves as it
# always did (runs live and die with the container).
log.info("persist.restore: %s", persist.restore(settings.db_path))

_store_singleton: Store | None = None

def store() -> Store:
    """The run store, opened once per process (telemetry.py's pattern)."""
    global _store_singleton
    if _store_singleton is None:
        _store_singleton = Store(settings.db_path)
    return _store_singleton

def remediate_and_verify(run_id: str, finding_id: str, market: str,
                         workdir=None, *, method: str = "auto",
                         replacement: str | None = None,
                         intent: str | None = None) -> bool:
    try:
        return _remediate_and_verify(run_id, finding_id, market, workdir,
                                     method=method, replacement=replacement,
                                     intent=intent)
    finally:
        # the edit, the change record and the verifier verdict all just
        # landed in the store; put them somewhere a new revision can find
        store().emit(run_id, "remediator", persist.snapshot(settings.db_path))


def _remediate_and_verify(run_id: str, finding_id: str, market: str,
                          workdir=None, *, method: str = "auto",
                          replacement: str | None = None,
                          intent: str | None = None) -> bool:
    """plan -> apply -> confirm, for one finding. Runs off the request thread.

    Everything is re-read from the store here rather than carried over from
    the request: the webhook's job is to name a finding, not to hand this
    function a state it has been holding.

    Held under remediate.market_lock for the whole plan-apply-verify span,
    not just the edit: the verifier re-observes the same localized master
    that apply() just wrote, so a second alert for the same market editing it
    in between would have the verifier judging a file nobody asked it about,
    and would revert the fix it is confirming. Starlette runs this sync
    function through run_in_threadpool, so two webhook calls really are two
    threads and a threading lock is the primitive that excludes them (an
    asyncio lock would not). See remediate.market_lock for the per-process
    ceiling.

    Never raises. A remediation that fails leaves the finding as the
    Remediator left it and records a stage error on the run, which is what
    the Mission Feed and customs_stage_error surface; the alert simply stays
    up, which is the honest signal that nothing was fixed.
    """
    db = store()
    workdir = Path(workdir) if workdir is not None else WORKDIR
    try:
        run = db.get_run(run_id)
        if run is None:
            log.warning("alert names run %s, which is not in the store", run_id)
            return False
        finding = next(
            (f for f in db.findings(run_id, market) if f.id == finding_id), None
        )
        if finding is None:
            log.warning("alert names finding %s, which is not in run %s",
                        finding_id, run_id)
            return False
        observation = next(
            (o for o in db.observations(run_id) if o.id == finding.observation_id), None
        )
        with remediate.market_lock(run_id, market):
            # "bridge" is the operator's call, never the planner's: it is
            # the only method that regenerates footage. "overlay" means
            # "patch it, and let plan() pick which kind of patch".
            # The violation's shape is recorded, not enforced. It used to
            # refuse the job, and at concept scope it refused nearly every
            # one: the verifier is what decides whether an edit actually
            # worked, and it is better at that than a rule of thumb about
            # shape. A poor fit is written to the feed so the operator can
            # see it was expected. See customs/scope.py.
            shape = scope_mod.classify(finding, db.findings(run_id),
                                       asset_duration(run) or 120.0)
            chosen = "bridge" if method == "bridge" else remediate.plan(finding, observation)
            fits, why = scope_mod.allows(shape,
                                         "bridge" if chosen == "bridge" else "overlay",
                                         finding.substitutable)
            if not fits:
                db.emit(run_id, "remediator",
                        f"{finding.rule_id} ({market}) -> {shape} scope, "
                        f"running {chosen} anyway: {why}")
            db.emit(run_id, "remediator",
                    f"{finding.rule_id} ({market}) -> planned {chosen}")
            span = max(0.0, finding.t_end - finding.t_start)

            def _charge(_db=db, _run=run_id, _fid=finding.id, _span=span) -> None:
                _db.record_spend("bridge", costs.estimate("bridge", _span),
                                 _run, _fid)

            change = remediate.apply(
                run, finding, chosen, workdir, db,
                replacement=replacement, intent=intent,
                statement=observation.statement if observation else "",
                spend=_charge if chosen == "bridge" else None,
                on_event=lambda agent, message: db.emit(run_id, agent, message))
            return verify.confirm(run, market, [change], db, workdir)
    except Exception as exc:  # noqa: BLE001 -- a background task has nobody to raise to
        log.exception("remediation of %s failed", finding_id)
        db.emit(run_id, "remediator", f"stage_error: remediate: {finding_id}: {exc!r}")
        return False

def _label(labels, key: str) -> str:
    value = labels.get(key)
    return value if isinstance(value, str) else ""

@app.post("/webhook/alert")
async def alert_webhook(request: Request, background: BackgroundTasks) -> dict:
    """Grafana alert contact point. Always 200, always fast.

    Reads alerts[].labels only. An alert whose labels name no open finding is
    logged and dropped: unknown asset, forged rule_id, an already-resolved
    finding and a resolved-status alert all land in that same branch, and
    none of them start any work.
    """
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 -- a malformed body is a dropped alert, not a 500
        log.warning("alert webhook received a body that is not JSON")
        return {"accepted": 0, "ignored": 0, "error": "body is not JSON"}

    alerts = payload.get("alerts") if isinstance(payload, dict) else None
    if not isinstance(alerts, list):
        log.warning("alert webhook received no alerts[] array")
        return {"accepted": 0, "ignored": 0}

    accepted = 0
    ignored = 0
    for alert in alerts:
        if not isinstance(alert, dict):
            ignored += 1
            continue
        # Grafana sends status "firing" or "resolved". A resolved alert means
        # the metric already dropped, so there is nothing to remediate.
        if (alert.get("status") or "firing") != "firing":
            ignored += 1
            continue
        labels = alert.get("labels")
        labels = labels if isinstance(labels, dict) else {}
        asset = _label(labels, "asset")
        market = _label(labels, "market")
        rule_id = _label(labels, "rule_id")
        if not (asset and market and rule_id):
            log.info("alert without {asset, market, rule_id} labels ignored")
            ignored += 1
            continue

        match = store().open_finding_by_labels(asset, market, rule_id)
        if match is None:
            log.info("alert labels {asset=%s, market=%s, rule_id=%s} match no open "
                     "finding; ignored", asset, market, rule_id)
            ignored += 1
            continue

        run, finding = match
        store().emit(run.id, "remediator",
                     f"alert received: {rule_id} {market} on {asset} -> {finding.id}")
        background.add_task(remediate_and_verify, run.id, finding.id, market)
        accepted += 1

    return {"accepted": accepted, "ignored": ignored}

@app.get("/healthz")
@app.get("/health")
async def healthz() -> dict:
    """Liveness. Deliberately touches nothing.

    Two paths for one handler because Google's run.app frontend swallows
    external requests to the literal path /healthz (it answers its own 404
    before the container is consulted; verified live 2026-08-24, GFE error
    page, zero request logs). Cloud Run's own probes hit the container
    directly, so /healthz still serves them; /health is the spelling that
    works from the public internet.
    """
    return {"status": "ok"}


# =========================================================================
# Customs Launch Control
# =========================================================================

# The two hard limits on an upload, from the design spec's own scope: a
# television commercial, not a feature. Both are enforced before a run record
# exists, so a rejected upload leaves nothing behind in runs/uploads/.
#
# What MAX_UPLOAD_BYTES actually bounds: create_run's chunk loop counts bytes
# as it copies the upload from Starlette's UploadFile into runs/uploads/, and
# by the time that loop runs, Starlette's multipart parser has already read
# the entire request body off the socket and spooled it to its own OS temp
# file (that spooling is what makes request.form() / UploadFile possible at
# all). So this check bounds what this process keeps in runs/uploads/, not
# what it is willing to receive: a client sending more than 200MB has already
# made the process accept and hold that many bytes on disk once, in
# Starlette's temp file, before this code gets a say. Acceptable for a
# laptop demo with no auth in front of it (Concern 3 in the task report);
# a public deployment would need the limit enforced on the receiving side,
# ahead of Starlette's own parser, not here.
MAX_UPLOAD_BYTES = 200 * 1024 * 1024
MAX_DURATION_S = 120.0
_UPLOAD_CHUNK = 1024 * 1024

# The mission feed polls the store this often and sends a comment line this
# often when nothing is happening. The heartbeat is what keeps a proxy from
# closing an idle SSE connection during the long quiet stretch while the
# analyst is inside a model call.
SSE_POLL_S = 0.25
SSE_HEARTBEAT_S = 15.0

# Which markets a tile can be in. "error" is not a clearance value, it is the
# absence of one: pipeline.errored_markets says the market was never judged,
# and drawing it as "cleared" would be the exact lie that function exists to
# prevent.
TILE_ORDER = {"blocked": 0, "at_risk": 1, "error": 2, "pending": 3, "cleared": 4}

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")

class _TooLarge(Exception):
    """The upload passed MAX_UPLOAD_BYTES mid-stream. Never leaves this module."""

def runs_root() -> Path:
    """runs/ -- where the store, the uploads and every run directory live.

    Derived from the store's own file, exactly as remediate.run_dir is, so a
    console pointed at a tmp database keeps its artifacts next to it.
    """
    return Path(store().db_path).parent

def run_dir(run) -> Path:
    """runs/{run_id}/ -- this run's artifacts, via the one definition of it."""
    return remediate.run_dir(run, store())

def uploads_dir() -> Path:
    return runs_root() / "uploads"

# -- reading a run --

def _run_or_404(run_id: str):
    run = store().get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"unknown run: {run_id}")
    return run

@lru_cache(maxsize=1)
def _packs_cached(_stamp: float):
    try:
        return packs.load()
    except packs.PackError as exc:
        log.error("market packs failed to load: %s", exc)
        return {}

def market_packs() -> dict:
    """The market packs, reloaded when the markets/ directory changes.

    Cached on the newest mtime under markets/ rather than forever: editing a
    pack during a demo and reloading the page should show the new rule, and
    re-reading three small YAML files per request would otherwise happen on
    every poll.
    """
    try:
        stamp = max(p.stat().st_mtime for p in Path("markets").glob("*.yaml"))
    except (OSError, ValueError):
        stamp = 0.0
    return _packs_cached(stamp)

def _judged_markets(db, run_id: str) -> set[str]:
    """Markets the guard has already published a clearance for.

    Read from the run's own event log ("{market} clearance -> {status}"),
    which is the same source pipeline.errored_markets reads, so "pending"
    means "no verdict yet" rather than "no findings yet". Without this a
    market that is still inside adjudicate.judge would draw as cleared the
    moment the page loaded, which is the single most dishonest thing this
    board could do.
    """
    seen = set()
    for _id, _ts, agent, message in db.events_since(run_id, 0):
        if agent == "adjudicator" and " clearance -> " in message:
            seen.add(message.split(" ", 1)[0])
    return seen

_LEVEL_ORDER = ("global", "continental", "national", "subnational", "channel")
_LEVEL_BLURB = {
    "global": "the baseline every market inherits",
    "continental": "what a continent adds on top",
    "national": "one country's law and self-regulation",
    "subnational": "a region with its own regime",
    "channel": "a broadcaster's own acceptance rules, on top of its country's",
}


def pack_groups() -> list[dict]:
    """The picker's shape: the jurisdiction ladder, channels under their country.

    Selecting a node means judging against that node's resolved rules, which
    packs.load has already flattened to own plus every ancestor's. So picking
    VRT judges against VRT, Belgium, the EU and the global baseline at once.
    """
    packs = market_packs()
    groups = []
    for level in _LEVEL_ORDER:
        members = sorted((p for p in packs.values() if p.level == level),
                         key=lambda p: (p.parent, p.market))
        if not members:
            continue
        by_parent: dict[str, list] = {}
        for pack in members:
            by_parent.setdefault(pack.parent, []).append(pack)
        groups.append({
            "level": level,
            "blurb": _LEVEL_BLURB.get(level, ""),
            "count": len(members),
            "families": [
                {"parent": parent, "parent_name": packs[parent].name if parent in packs else "",
                 "packs": items}
                for parent, items in sorted(by_parent.items())
            ],
        })
    return groups


_SHOTS_RE = re.compile(r"merged to (\d+)")


def run_progress(run, states: dict[str, dict]) -> dict:
    """How far along a clearance is, from the crew's own events.

    There is no progress counter to read: the run is a pipeline of stages
    whose sizes are only known once the stage before it finished (nobody
    knows the shot count until ingest has cut the film). So progress is
    inferred from what the agents have already said they did, weighted by
    how long each stage actually takes: the analyst is most of the wall
    clock, the adjudicators are cheap and parallel, and the publisher is a
    handful of calls at the end.
    """
    if run.status in ("done", "error"):
        return {"pct": 100, "stage": "done"}

    rows = store().events_since(run.id, 0)
    shots, transcribed, observed, judged, published = 0, 0, 0, 0, False
    for _id, _ts, agent, message in rows:
        if agent == "ingest":
            found = _SHOTS_RE.search(message)
            if found:
                shots = int(found.group(1))
        elif agent == "transcription":
            transcribed += 1
        elif agent == "analyst" and message.startswith("observe ->"):
            observed += 1
        elif agent == "adjudicator" and "clearance ->" in message:
            judged += 1
        elif agent == "publisher" and message.startswith("push_run_telemetry"):
            published = True

    markets = max(1, len(run.markets))
    # ingest is done the moment we know the shot count
    pct = 4 if not shots else 10
    if shots:
        pct += 22 * min(1.0, transcribed / shots)
        pct += 44 * min(1.0, observed / shots)
    pct += 20 * min(1.0, judged / markets)
    if published:
        pct = max(pct, 96)

    if published:
        stage = "publishing to Grafana"
    elif judged:
        stage = f"judging markets, {judged} of {markets} back"
    elif observed and shots:
        stage = f"watching the film, shot {min(observed, shots)} of {shots}"
    elif transcribed and shots:
        stage = f"transcribing, {min(transcribed, shots)} of {shots}"
    elif shots:
        stage = f"{shots} shots detected"
    else:
        stage = "detecting shots"
    return {"pct": int(min(99, max(2, round(pct)))), "stage": stage}


def _lanes_from_grafana(run) -> dict[str, list]:
    """The lanes, out of Loki.

    Every observation is a line there now, carrying its dimension as a
    label and its timecode, its flagged state, its severity and the
    markets that objected in the body. That is the whole chart, so this
    is the chart's real source: Grafana holds the data, the app draws it,
    exactly like the stat cards on the tiles.

    Returns {} rather than raising, so a dead Grafana costs the chart its
    provenance, not its existence -- the store still has the same facts.
    """
    asset = Path(run.asset_path).stem or run.asset_path
    query = (f'{{app="customs", kind="observation", '
             f'asset="{re.sub(chr(34), "", asset)}"}} | json')
    try:
        from customs.grafana_ops import GrafanaOps
        duration = asset_duration(run) or MAX_DURATION_S
        with GrafanaOps(settings) as ops:
            rows = ops.loki_lines(query, limit=2000,
                                  start=(run.t0 or 0) - 2,
                                  end=(run.t0 or 0) + duration + 2)
    except Exception as exc:  # noqa: BLE001 -- the store has the same facts
        log.warning("lanes from Loki failed for %s: %s", run.id, exc)
        return {}
    lanes: dict[str, list] = {}
    for row in rows:
        body = row.get("parsed") or {}
        if body.get("run_id") != run.id:
            continue
        dimension = body.get("dimension")
        if not dimension or dimension == "none":
            continue
        markets = body.get("markets") or []
        lanes.setdefault(dimension, []).append({
            "t": float(body.get("t_start") or 0.0),
            "flagged": bool(body.get("findings")),
            "severity": int(body.get("max_severity") or 0),
            "obs": body.get("observation_id") or "",
            "market": ", ".join(markets[:4]) if isinstance(markets, list) else "",
        })
    return lanes


def _lanes_from_store(run) -> dict[str, list]:
    """The same lanes from SQLite, for a run older than the Loki history."""
    db = store()
    by_obs: dict[str, list] = {}
    for f in db.findings(run.id):
        by_obs.setdefault(f.observation_id, []).append(f)
    lanes: dict[str, list] = {}
    for obs in db.observations(run.id):
        if not obs.dimension:
            continue
        hits = by_obs.get(obs.id, [])
        lanes.setdefault(obs.dimension, []).append({
            "t": obs.t_start, "flagged": bool(hits),
            "severity": max((f.severity for f in hits), default=0),
            "obs": obs.id,
            "market": ", ".join(sorted({f.market for f in hits})[:4]),
        })
    return lanes


@lru_cache(maxsize=1)
def _sprite_symbols() -> dict[str, str]:
    """Every <symbol> in base.html's sprite, by id.

    Read once from the template rather than duplicated here, so an icon
    redrawn in the sprite is redrawn everywhere it is used.
    """
    src = (_HERE / "templates" / "base.html").read_text()
    return {m.group(1): m.group(0) for m in
            re.finditer(r'<symbol id="([^"]+)".*?</symbol>', src, re.S)}


def _sprite_defs(ids) -> str:
    """A <defs> carrying exactly the symbols these ids name, and no more.

    A chart served as its own file cannot reach the sprite in the page
    that embeds it, so it has to carry what it uses. Shipping the whole
    sprite would put fifty symbols in every card; a lane chart needs six.
    """
    have = _sprite_symbols()
    wanted = [have[i] for i in dict.fromkeys(ids) if i in have]
    return f"<defs>{''.join(wanted)}</defs>" if wanted else ""


def problem_lanes(run, compact: bool = False) -> str:
    """Where in the film each KIND of problem happens, as one lane each.

    The board says which markets are unhappy and the frame board says
    what the analyst saw, but nothing said WHEN -- whether a run has one
    bad shot or trouble throughout. This is that view: a lane per
    dimension, the ad's own clock across the bottom, a dot at every
    observation, filled and coloured where a market objected and faint
    where nobody did.

    Faint dots are the point as much as the loud ones. A lane thick with
    pale dots and one red one says "we look at this constantly and it is
    almost always fine", which is exactly the negative space the verdict
    record exists to make visible.
    """
    lanes = _lanes_from_grafana(run) or _lanes_from_store(run)
    if not lanes:
        return ""
    # worst first, so the lane that blocks a market is the top line
    rows = [{"dimension": d, "events": sorted(e, key=lambda x: x["t"])}
            for d, e in sorted(lanes.items(),
                               key=lambda kv: -max((x["severity"] for x in kv[1]),
                                                   default=0))]
    if compact:
        # A card is not a page. Six lanes is what fits under a thumbnail
        # without the card becoming a chart with a title, and they are the
        # six that matter because rows are already worst-first.
        rows = rows[:6]
        # A card chart is drawn at 560 and displayed at about 330, so an
        # icon is on screen at roughly six tenths of the size it is
        # written at. At 20 that put the taxonomy glyphs at ~12px, which
        # is smaller than the pills beside them and too small to tell a
        # wine glass from a dress. Double, with the gutter widened to
        # hold them.
        return spark.lanes(rows, asset_duration(run) or MAX_DURATION_S,
                           width=560, row_h=46, pad_left=54, ruler=False,
                           icon=38,
                           defs=_sprite_defs(f"d-{r['dimension']}" for r in rows))
    return spark.lanes(rows, asset_duration(run) or MAX_DURATION_S,
                       defs=_sprite_defs(f"d-{r['dimension']}" for r in rows))


def _kinds_found(findings) -> list[str]:
    """Which KINDS of problem a market found, worst first.

    A Finding carries the rule it broke, not the dimension -- dimension is
    a property of the Observation -- so this resolves it the same way
    telemetry does, through the market pack. Deduplicated, because a
    market objecting three times about dress is still one kind of problem,
    and ordered by worst severity so a tile leads with what matters.
    """
    from customs.telemetry import _dimension_for
    worst: dict[str, int] = {}
    for f in findings:
        dimension = _dimension_for(f.market, f.rule_id)
        if not dimension or dimension == "none":
            continue
        worst[dimension] = max(worst.get(dimension, 0), f.severity)
    return [d for d, _ in sorted(worst.items(), key=lambda kv: -kv[1])]


def market_states(run) -> dict[str, dict]:
    """Per market: {clearance, findings, blocked, errored} for one run.

    `clearance` is adjudicate.clearance's verdict over that market's stored
    findings -- the same status-aware function the CLI and the metrics use --
    or "pending" while the market has no verdict and the run is still going.
    `errored` is pipeline.errored_markets: it does not change the clearance
    value, it says the value cannot be trusted, and every screen draws ERROR
    instead of the verdict when it is set.
    """
    db = store()
    errored = pipeline.errored_markets(db, run.id)
    judged = _judged_markets(db, run.id)
    finished = run.status in ("done", "error")
    states = {}
    for market in run.markets:
        findings = db.findings(run.id, market)
        decided = finished or market in judged or market in errored
        # "cleared" means nothing OPEN is disqualifying, which is not the
        # same as nothing being wrong: offence-class findings, unsourced ones
        # and anything under the severity threshold stay open and stay
        # visible. The tile carries that count so the word never stands
        # alone, and a market with an edit in flight says so rather than
        # reporting the verdict it would have if the edit worked.
        states[market] = {
            "clearance": adjudicate.clearance(findings) if decided else "pending",
            "findings": len(findings),
            "open": sum(1 for f in findings if f.status == "open"),
            "working": sum(1 for f in findings if f.status == "remediating"),
            "resolved": sum(1 for f in findings if f.status == "resolved"),
            "blocked": sum(1 for f in findings if f.remediation_blocked),
            "errored": market in errored,
            # WHICH KINDS of problem this market found, as the taxonomy's
            # own icons. A count says a market is unhappy; these say what
            # about. Ordered by worst severity first so the tile leads with
            # the thing that matters, and deduplicated because one market
            # objecting three times over dress is still one kind of problem.
            "kinds": _kinds_found(findings),
        }
        states[market]["display"] = tile_state(states[market])
    return states

def tile_state(state: dict) -> str:
    """The one word a tile is drawn with.

    Four things can be true at once and only one of them fits on a badge, so
    the order is: an unevaluated market first, then an edit in flight, then
    the "cleared but not clean" case, then the verdict itself.

    That third one is why this function exists. clearance() says "cleared"
    when nothing OPEN disqualifies the market, and offence findings, unsourced
    ones and anything under severity 70 never disqualify anything. So a market
    could carry two open findings and wear a green CLEARED badge, which reads
    as a contradiction to the only people who matter here. It gets its own
    state instead: cleared to air, with things still on the table.
    """
    if state["errored"]:
        return "error"
    if state.get("working"):
        return "pending"
    if state["clearance"] == "cleared" and state.get("open"):
        return "noted"
    return state["clearance"]

def overall(states: dict[str, dict]) -> dict:
    """The headline: how many markets are cleared, and which are not.

    A market is cleared only if its verdict says so AND it was really
    evaluated. Everything else -- blocked, at risk, errored, still pending --
    is named in `failing`, in the run's own market order, because a headline
    that says "2 of 3" without saying which one is missing is a scoreboard,
    not a verdict.
    """
    cleared = [m for m, s in states.items()
               if s["clearance"] == "cleared" and not s["errored"]]
    failing = [m for m in states if m not in cleared]
    pending = any(s["clearance"] == "pending" and not s["errored"]
                  for s in states.values())
    if pending:
        state = "pending"
    elif failing:
        state = "no_go"
    else:
        state = "go"
    return {"cleared": len(cleared), "total": len(states),
            "failing": failing, "state": state}

def published(run) -> bool:
    """Has the Publisher pushed this run's telemetry into Grafana yet?

    The panels are empty until it has (it is the last stage of the run), and
    a Grafana panel reading "No data" on a board that is still working says
    something false about the run. The board shows what is actually happening
    instead: the Publisher has not run yet.

    Read from run.t0 rather than from the run's status, because t0 is what
    telemetry.push_timeline rewrites when it maps the clock, and it is
    exactly the value the panel window is built from.
    """
    if run.t0 is None:
        return False
    return any(agent == "publisher" and "push_run_telemetry" in message
               for _id, _ts, agent, message in store().events_since(run.id, 0))

@lru_cache(maxsize=64)
def _duration_of(path: str, _mtime: float) -> float:
    try:
        return probe_duration(path)
    except (MediaError, OSError, ValueError):
        return MAX_DURATION_S

def asset_duration(run) -> float:
    """The asset's duration, probed once per file per process.

    Used for the timeline embed window and for the asset strip. ffprobe in a
    request handler would be rude on every poll, hence the cache keyed on the
    file's mtime; a missing or unreadable file falls back to the 120s cap
    rather than raising a page.
    """
    try:
        mtime = Path(run.asset_path).stat().st_mtime
    except OSError:
        mtime = 0.0
    return _duration_of(str(run.asset_path), mtime)

def embeds(run) -> dict[str, str]:
    """The two Grafana pages the board links out to, windowed for this run.

    Pure string building, no network call and no GrafanaOps: see
    config._PUBLIC_DASHBOARDS for why the share URLs are pinned instead of
    discovered, and grafana_ops.embed_url for the same rule applied to the
    per-panel form of these URLs.

    The two windows are deliberately different, because the two pages sit on
    different clocks (telemetry.py's module docstring is the reference):

      overview   now-6h..now      status metrics are stamped at the real
                                  clock, and they move again every time a
                                  remediation resolves a finding, so the
                                  page has to follow the present.
      timeline   t0..t0+duration  the risk series is written on the run's
                                  mapped clock, where wall time t0+n IS video
                                  second n, so this window is the ad's own
                                  timecode and nothing else.
    """
    overview = f"{settings.grafana_public_overview}?from=now-6h&to=now"
    # The lane panel is not one of the two public pages, so this one lands on
    # the stack itself and asks the operator to be logged in. It was a
    # host-relative /d/customs-lanes before, which is a path on THIS app --
    # a chip that could only ever 404. Its variables are filled in from the
    # run so the panel opens showing exactly the picture on the board.
    asset = Path(run.asset_path).stem or run.asset_path
    lanes = (f"{settings.grafana_url.rstrip('/')}/d/customs-lanes/customs"
             f"?var-asset={quote(asset)}&var-run={quote(run.id)}")
    if run.t0 is None:
        return {"overview": overview,
                "timeline": f"{settings.grafana_public_timeline}?from=now-3h&to=now",
                "lanes": lanes}
    duration = asset_duration(run)
    start_ms = int((run.t0 - 5) * 1000)
    end_ms = int((run.t0 + duration + 5) * 1000)
    return {
        "overview": overview,
        "timeline": f"{settings.grafana_public_timeline}?from={start_ms}&to={end_ms}",
        "lanes": f"{lanes}&from={start_ms}&to={end_ms}",
    }

# --- the instrument panel ------------------------------------------------
#
# The design spec pinned this as the most likely thing to break late, and it
# broke exactly there: "An iframe cannot carry a bearer token and Grafana
# Cloud has no anonymous access, so embedding is an auth problem." Public
# dashboards solved the auth half -- those URLs open with no login, and the
# board links to them -- but this stack answers every request, public
# dashboards included, by refusing to be framed. The exact mechanism is in
# spark.py's closing note and reproducible with scripts/probe_framing.py:
# a GET -- which is what a browser's iframe issues -- comes back with CSP
# `frame-ancestors 'none'` and no x-frame-options, while a HEAD comes back
# with `x-frame-options: deny` and no CSP. An earlier version of this
# comment read the HEAD and concluded the stack sets no frame-ancestors
# directive. It does, and it is the one that decides. A browser refuses to
# frame that, and the page gets an empty box.
#
# So the console runs the spec's own fallback, which was built at the same
# time as the primary for this reason: "server-side panel rendering through
# the image renderer API using a service account token, which loses
# interactivity but cannot fail on panel type support". GrafanaOps.render_png
# does the render with the same service account the Publisher agent used, the
# console caches the PNG next to the run's other artifacts, and every panel
# links to the live public dashboard for the interactive version. What is on
# the board is a real panel with this run's real data, not a picture of one
# taken earlier.

PANELS = {
    "clearance": {"uid": "customs-overview", "panel": 1, "clock": "current",
                  "width": 1200, "height": 260},
    "timeline": {"uid": "customs-timeline", "panel": 1, "clock": "mapped",
                 "width": 1200, "height": 420},
}
# How long a rendered panel is served before it is rendered again. A render is
# six seconds of Grafana, and the board is polled every two: without a cache
# the page would queue renders faster than they complete.
PANEL_CACHE_S = 45.0

# Guards the check-then-render-then-write sequence in panel_png below. Two
# requests for the same run's panel landing inside the same instant (the
# board's own two-panel layout loading together, or a poll racing a reload)
# must not both decide the cache file is stale and both render and write it:
# one write would step on the other mid-write and a reader could be handed a
# torn PNG. One lock per (run_id, name), the same shape as
# remediate.market_lock: a dict guarded by its own lock so creating an entry
# is itself race-free, rather than one lock wide enough to serialize panels
# that have nothing to do with each other.
_panel_locks: dict[tuple[str, str], threading.Lock] = {}
_panel_locks_guard = threading.Lock()

def _panel_lock(run_id: str, name: str) -> threading.Lock:
    key = (run_id, name)
    with _panel_locks_guard:
        lock = _panel_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _panel_locks[key] = lock
        return lock

def _render_panel(run, spec) -> bytes:
    """One panel as PNG, over the window its clock calls for.

    mcp_tools=set() is deliberate: it skips the mcp-grafana subprocess
    entirely (GrafanaOps documents the injected-inventory path) and takes the
    HTTP renderer, because spawning a subprocess per page view to render an
    image would be absurd.
    """
    from customs.grafana_ops import GrafanaOps  # local: pulls the MCP client

    if spec["clock"] == "current":
        duration = max(time.time() - run.t0, 60.0)
    else:
        duration = asset_duration(run) + 5
    ops = GrafanaOps(settings, mcp_tools=set())
    return ops.render_png(spec["uid"], spec["panel"], run, duration=duration,
                          width=spec["width"], height=spec["height"])

@app.get("/runs/{run_id}/changes/{change_id}/generated.mp4")
def generated_clip(run_id: str, change_id: str):
    """The footage a model invented, exactly as it came back.

    A bridge splices generated seconds into the master, and the master is
    what ships, but the raw clip is what a brand actually wants to watch
    before signing anything: it is the only part of the film nobody shot.
    Kept beside the change record's stills, so it survives a deploy like
    everything else there.
    """
    run = _run_or_404(run_id)
    if not re.fullmatch(r"chg_[0-9a-f]{6,32}", change_id):
        raise HTTPException(status_code=404, detail="no such change")
    changes = run_dir(run) / "changes"
    clip = changes / f"{change_id}_bridge.mp4"
    if not clip.is_file():
        # Bridges written before 2026-08-25 all landed on one hidden file,
        # ".chg_bridge.mp4", because the name came from the staged master
        # rather than the change record. Only the newest survives per run,
        # but it is a real generated clip and worth serving.
        legacy = changes / ".chg_bridge.mp4"
        if legacy.is_file():
            return FileResponse(legacy, media_type="video/mp4")
    if not clip.is_file():
        raise HTTPException(status_code=404,
                            detail="no generated clip for this change")
    return FileResponse(clip, media_type="video/mp4")


@app.get("/grafana/{uid}.png")
def grafana_png(uid: str, run: str = ""):
    """A whole Grafana dashboard, rendered server-side as an image.

    Grafana Cloud answers every page with `x-frame-options: deny + frame-ancestors 'none'`, so a
    dashboard cannot be put in an iframe: the browser refuses the connection
    and the panel comes back blank, which is exactly what agent mode did
    when it handed back the URL of a dashboard it had just built. The board
    has always solved this by rendering panels through Grafana's own
    renderer with the service account token, and a dashboard the agent
    composed is no different.
    """
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,60}", uid):
        raise HTTPException(status_code=404, detail="no such dashboard")
    now_ms = int(time.time() * 1000)
    try:
        from customs.grafana_ops import GrafanaOps  # local: pulls the MCP client
        with GrafanaOps(settings) as ops:
            # Light, and wide. The pane this lands in is the right half of
            # a desktop window, so the render is sized to fill it rather
            # than sit as a small dark card in a light console.
            png = ops.render_png(uid, None, None, None, width=1600, height=900,
                                 theme="light",
                                 window_ms=(now_ms - 24 * 3600 * 1000, now_ms))
    except Exception as exc:  # noqa: BLE001 -- a dead renderer is not a 500 here
        log.warning("dashboard render failed for %s: %s", uid, exc)
        raise HTTPException(status_code=502,
                            detail="Grafana would not render that dashboard") from exc
    return Response(content=png, media_type="image/png",
                    headers={"Cache-Control": "public, max-age=60"})


@app.get("/runs/{run_id}/panels/{name}.png")
def panel_png(run_id: str, name: str):
    """A Grafana panel for this run, rendered server-side and cached on disk.

    Falls back down a ladder rather than failing the page: a fresh cache file
    is served as is, a stale one is re-rendered, and a render that fails with
    a stale file on disk serves the stale file (an expired panel is worth more
    than a broken image). Only a render that fails with nothing cached 404s,
    which the board turns into a link to the live dashboard.

    The check, the render and the write are all done under _panel_lock(name,
    run_id): see that function for why two requests landing together must
    not both render this same file at once.
    """
    run = _run_or_404(run_id)
    spec = PANELS.get(name)
    if spec is None or run.t0 is None:
        raise HTTPException(status_code=404, detail="no such panel for this run")

    cached = run_dir(run) / "panels" / f"{name}.png"
    with _panel_lock(run_id, name):
        fresh = cached.is_file() and (time.time() - cached.stat().st_mtime) < PANEL_CACHE_S
        if not fresh:
            try:
                png = _render_panel(run, spec)
                cached.parent.mkdir(parents=True, exist_ok=True)
                cached.write_bytes(png)
            except Exception as exc:  # noqa: BLE001 -- Grafana being down is not a 500 here
                log.warning("panel render failed for %s/%s: %s", run.id, name, exc)
                if not cached.is_file():
                    raise HTTPException(status_code=404,
                                        detail="panel could not be rendered") from None
    return FileResponse(cached, media_type="image/png",
                        headers={"Cache-Control": f"max-age={int(PANEL_CACHE_S)}"})

# -- template helpers --

def _timecode(seconds) -> str:
    """Video seconds as mm:ss.d, the way a timeline reads them."""
    try:
        seconds = float(seconds)
    except (TypeError, ValueError):
        return "--:--"
    minutes, rest = divmod(max(seconds, 0.0), 60)
    return f"{int(minutes):02d}:{rest:04.1f}"

def _clock(ts) -> str:
    """A unix timestamp as a local wall clock with tenths, for log lines."""
    try:
        ts = float(ts)
    except (TypeError, ValueError):
        return "--:--:--"
    return time.strftime("%H:%M:%S", time.localtime(ts)) + f".{int(ts % 1 * 10)}"

def _stamp(ts) -> str:
    if not ts:
        return "not started"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(ts)))

templates.env.filters["timecode"] = _timecode
templates.env.filters["clock"] = _clock
templates.env.filters["stamp"] = _stamp

_LEVEL_ROWS = ("global", "continental", "national", "subnational", "channel")


def market_rows(run) -> list[dict]:
    """This run's markets, grouped into one row per jurisdiction level.

    A run can cover a global baseline, a continent, a dozen countries and
    twenty broadcasters at once, and a single strip of tabs makes that look
    like one flat list of codes. One row per level, labelled, says what you
    are actually looking at.
    """
    all_packs = market_packs()
    # The tabs carry the verdict as a coloured underline. Without it the
    # only way to see that a channel is blocking was to scroll past the
    # tiles, which is the wrong way round: the tab strip is what you steer
    # by, and it was the one part of the board saying nothing.
    states = market_states(run)
    rows = []
    for level in _LEVEL_ROWS:
        codes = [m for m in run.markets
                 if (all_packs[m].level if m in all_packs else "national") == level]
        if codes:
            rows.append({"level": level, "markets": [
                {"code": c, "state": tile_state(states[c]) if c in states else "pending"}
                for c in sorted(codes)]})
    return rows


def clearance_gauge(states: dict) -> str:
    """How much is still open before this run clears, as a gauge.

    Open findings across every market on the run, against everything it
    raised. Empty means cleared and clean; a full arc means nothing has
    been dealt with yet.

    Coloured by the worst thing standing: red while a market is blocked,
    amber while a cleared market still carries open findings, green when
    there is nothing left.
    """
    open_n = sum(m.get("open", 0) for m in states.values())
    total = sum(m.get("findings", 0) for m in states.values())
    if any(m.get("clearance") == "blocked" for m in states.values()):
        colour = state_mod.BLOCKED
    elif open_n:
        colour = state_mod.AT_RISK
    else:
        colour = state_mod.CLEARED
    return spark.gauge(open_n, total, colour=colour)


def pill_groups(run, states: dict) -> list[dict]:
    """A run card's market pills, split into where and on what.

    A card used to carry one flat run of codes, so `ID` sat next to
    `CAQC-NOOVO` with nothing to say that one is a country and the other
    is a broadcaster inside a different country. They answer different
    questions -- which territories is this cleared for, and which
    schedules will actually take it -- so they are two groups now.

    Everything above a broadcaster is territory, however deep the ladder
    goes, which is why this is a two-way split and not one row per level:
    a card has room to say "geo" and "channel", not five words.
    """
    all_packs = market_packs()
    geo, channel = [], []
    for code, state in states.items():
        level = all_packs[code].level if code in all_packs else "national"
        (channel if level == "channel" else geo).append((code, state))
    out = []
    for label, items in (("geo", geo), ("channel", channel)):
        if items:
            # NOT "items": Jinja resolves group.items to dict.items, the
            # bound method, and iterating that is a TypeError at render.
            out.append({"label": label,
                        "markets": [{"code": c, "state": tile_state(st)}
                                    for c, st in sorted(items)]})
    return out


def _page(request: Request, name: str, **context):
    return templates.TemplateResponse(request, name, context)


templates.env.globals["market_rows"] = market_rows

# -- the front door --

# Who came through the door. Nothing is gated on it yet -- it is written
# so that when something is (a reserved generation budget, a read-only
# mode), the console already knows which of the two it is talking to
# rather than having to ask on the way past.
ROLES = {
    "judge": "Devpost judge",
    "visitor": "Curious visitor",
}


# A visitor's own runs, kept in their browser rather than in the store.
#
# The alternative was a column on the runs table and a migration, for a
# demo where "whose run is this" has no security meaning: nothing is
# hidden, every run is still reachable by its URL, and the archive is a
# reading convenience rather than a boundary. So the list of run ids
# lives in the cookie that created them.
MINE_COOKIE = "customs-mine"
ROLE_COOKIE = "customs-role"
MINE_MAX = 40


def _role(request: Request) -> str:
    return request.cookies.get(ROLE_COOKIE, "")


def _mine(request: Request) -> list[str]:
    raw = request.cookies.get(MINE_COOKIE, "")
    return [r for r in (x.strip() for x in raw.split(",")) if r]


@app.get("/", response_class=HTMLResponse)
def landing(request: Request):
    """The front door: what this is, before what it does.

    Everything behind this page assumes you already know what ad
    clearance is and why fifteen markets disagree about a glass of wine.
    Someone arriving from a submission link does not, and the first thing
    they used to meet was an upload form asking for a master they do not
    have. So the form moved to /new and this says what the thing IS.
    """
    return _page(request, "landing.html", screen="landing",
                 roles=ROLES,
                 packs_total=len(market_packs()),
                 rules_total=sum(len(p.own_rules) for p in market_packs().values()),
                 dims_total=len(packs.taxonomy()),
                 runs_total=len(store().recent_runs(500)))


@app.get("/enter/{role}")
def enter(role: str):
    """Come in as one or the other, and be remembered for it.

    Not authentication and not pretending to be: there is no secret, and
    either door opens. It records which one you chose, which is the hook
    a budget split or a read-only mode would hang off later.

    A judge lands on the archive, because the work is already done and
    the interesting thing is reading it. A visitor lands on the form,
    because the interesting thing is watching it happen to their own ad.
    """
    if role not in ROLES:
        raise HTTPException(status_code=404, detail=f"unknown door: {role}")
    target = "/runs" if role == "judge" else "/new"
    response = RedirectResponse(target, status_code=303)
    response.set_cookie("customs-role", role, max_age=60 * 60 * 24 * 30,
                        samesite="lax", httponly=False)
    return response


@app.get("/new", response_class=HTMLResponse)
def home(request: Request):
    """The upload form. Nothing else.

    It used to carry the last twelve runs underneath, so the page did two
    unrelated jobs and the run list was a scroll away from a form nobody
    was filling in. Reading the history is now its own tab (/runs), which
    is also the one that has the card/list toggle.

    It used to be at /, which is now the page that explains the product
    to someone who has never seen it.
    """
    return _page(request, "home.html", groups=pack_groups(), screen="home")

@app.post("/runs")
async def create_run(request: Request,
                     asset: UploadFile | None = File(None),
                     youtube_url: str = Form(""),
                     markets: list[str] = Form(default=[])):
    """Accept an asset, create the run, start the crew, redirect to the board.

    Order matters here. The upload is streamed to disk with a running byte
    count, then probed, and only an asset that passes both limits gets a run
    record. A rejected upload leaves no run, no directory and no file in
    runs/uploads/: the reply is a plain text 400 saying which limit it hit,
    because this is the one place in the console where the user is told they
    did something wrong and a styled error page would be slower to read than
    the sentence. (What that byte count does and does not bound is on
    MAX_UPLOAD_BYTES above: it is a storage limit, not a receive limit.)

    This handler is async and runs on the event loop, so the write loop below
    and the probe_duration call after it are both handed to
    asyncio.to_thread -- the same pattern mission_stream uses to poll the
    store off the loop. A 200MB write or a multi-second ffprobe call done
    directly here would block every other client's poll and SSE feed for as
    long as it took.

    The crew then runs on a thread. See this module's docstring for why that
    is a thread and not a BackgroundTask.
    """
    known = set(market_packs())
    chosen = [m for m in markets if m]
    if not chosen:
        return PlainTextResponse("Select at least one market to clear for.",
                                 status_code=400)
    unknown = [m for m in chosen if m not in known]
    if unknown:
        return PlainTextResponse(
            f"No market pack for: {', '.join(unknown)}. "
            f"Known markets: {', '.join(sorted(known))}.", status_code=400)

    url = youtube_url.strip()
    has_file = asset is not None and bool((asset.filename or "").strip())
    if url and has_file:
        return PlainTextResponse(
            "Provide either an uploaded master or a YouTube link, not both.",
            status_code=400)
    if not url and not has_file:
        return PlainTextResponse(
            "Provide a master: upload a file or paste a YouTube link.",
            status_code=400)

    # One directory per upload, keeping the file's own name inside it. The
    # name matters beyond tidiness: telemetry labels every metric and every
    # alert with the asset path's *stem*, so a uniquifying prefix on the
    # filename would follow this asset onto the dashboards and into the alert
    # labels as "a1b2c3_spring_launch". The directory carries the uniqueness
    # instead, and two uploads of the same filename still cannot collide.
    folder = uploads_dir() / uuid.uuid4().hex[:12]
    folder.mkdir(parents=True, exist_ok=True)

    if url:
        # The YouTube way in: fetch.fetch_youtube validates the link, refuses
        # a too-long video before downloading, caps the download, and raises
        # FetchError with a sentence made for this 400. It runs in a thread
        # for the same reason the write loop below does: it is network plus
        # an ffmpeg merge, and the event loop serves everyone else meanwhile.
        try:
            target = await asyncio.to_thread(
                fetch_youtube, url, folder, MAX_DURATION_S, MAX_UPLOAD_BYTES)
        except FetchError as exc:
            for leftover in folder.glob("*"):
                leftover.unlink(missing_ok=True)
            try:
                folder.rmdir()
            except OSError:
                pass
            return PlainTextResponse(str(exc), status_code=400)
        safe = target.name
    else:
        safe = _SAFE_NAME.sub("_", Path(asset.filename or "asset.mp4").name)[-60:]
        target = folder / safe

        size = 0
        try:
            with target.open("wb") as out:
                while chunk := await asset.read(_UPLOAD_CHUNK):
                    size += len(chunk)
                    if size > MAX_UPLOAD_BYTES:
                        raise _TooLarge
                    await asyncio.to_thread(out.write, chunk)
        except _TooLarge:
            _discard(target)
            return PlainTextResponse(
                f"That file is too large. The limit is "
                f"{MAX_UPLOAD_BYTES // (1024 * 1024)} MB and this upload is over it.",
                status_code=400)

    try:
        duration = await asyncio.to_thread(probe_duration, str(target))
    except Exception as exc:  # noqa: BLE001 -- an unreadable upload is a 400, not a 500
        _discard(target)
        return PlainTextResponse(
            f"That file could not be read as video: {exc}", status_code=400)
    if duration > MAX_DURATION_S:
        _discard(target)
        return PlainTextResponse(
            f"That asset is {duration:.0f} seconds long. Customs clears "
            f"commercials up to {int(MAX_DURATION_S)} seconds.", status_code=400)

    run = store().create_run(asset_path=str(target), markets=chosen)
    store().emit(run.id, "pipeline",
                 f"console accepted {safe} ({duration:.1f}s) for "
                 f"{', '.join(chosen)}")
    threading.Thread(target=_clearance_job, args=(run.id, str(target), chosen),
                     name=f"clearance-{run.id}", daemon=True).start()
    response = RedirectResponse(f"/runs/{run.id}", status_code=303)
    # Newest first, capped: a cookie is not a database and forty run ids
    # is already more archive than anyone builds in a sitting.
    remembered = [run.id] + [r for r in _mine(request) if r != run.id]
    response.set_cookie(MINE_COOKIE, ",".join(remembered[:MINE_MAX]),
                        max_age=60 * 60 * 24 * 30, samesite="lax")
    return response

def _discard(target: Path) -> None:
    """Undo a rejected upload: the file, then the directory it was alone in."""
    target.unlink(missing_ok=True)
    try:
        target.parent.rmdir()
    except OSError:  # not empty, or already gone
        pass

def launch_clearance(run_id: str, asset_path: str, markets: list[str],
                     workdir=None) -> None:
    """Run the crew for a run record that already exists.

    The one seam between the console and the agent graph, and the only place
    the console imports ADK. The import is local because it costs seconds and
    pulls the whole ADK dependency tree: the webhook, the tests and every
    read-only screen must not pay for it.
    """
    from customs import crew  # local: importing ADK costs seconds

    crew.run_clearance(asset_path, markets, store(), workdir or WORKDIR,
                       run_id=run_id)

def _clearance_job(run_id: str, asset_path: str, markets: list[str]) -> None:
    """Thread body: run the crew, and never let it die silently.

    crew.run_clearance swallows every stage error itself, with one documented
    exception (an asset with no detectable shots raises out of ingest). That
    exception is exactly the one a stranger's upload is most likely to hit,
    so it is caught here and written to the run as a stage error: the board
    then shows a run that stopped and says why, instead of tiles that pulse
    pending forever.

    Passes its own runs/work/{run_id} down into launch_clearance rather than
    letting it fall back to the shared WORKDIR: see WORKDIR's own comment for
    why two runs cannot share that scratch space.
    """
    db = store()
    try:
        launch_clearance(run_id, asset_path, markets, workdir=WORKDIR / run_id)
        db.emit(run_id, "pipeline", persist.snapshot(settings.db_path))
    except Exception as exc:  # noqa: BLE001 -- a thread has nobody to raise to
        log.exception("clearance run %s failed", run_id)
        db.emit(run_id, "pipeline", f"stage_error: run: {exc!r}")
        try:
            db.set_run_status(run_id, "error")
        except ValueError:
            pass

def _add_analysis_job(run_id: str, markets: list[str], duration: float) -> None:
    """Thread body for a second clearance pass. Mirrors _clearance_job."""
    db = store()
    try:
        run = db.get_run(run_id)
        pipeline.judge_more(db, run, markets, duration)
        db.emit(run_id, "pipeline", persist.snapshot(settings.db_path))
    except Exception as exc:  # noqa: BLE001 -- a thread has nobody to raise to
        log.exception("add analysis on %s failed", run_id)
        db.emit(run_id, "pipeline", f"stage_error: add analysis: {exc!r}")

@app.post("/runs/{run_id}/analysis")
def add_analysis(run_id: str, markets: list[str] = Form(default=[])):
    """Clear an existing run against more markets, reusing its observations.

    The screenshots, the transcript and the analyst's reading of them are
    already in the store and describe the film, not a jurisdiction. So this
    starts at the adjudicator: no download, no shot detection, no keyframes,
    no vision calls. See pipeline.judge_more.
    """
    run = _run_or_404(run_id)
    if not store().observations(run.id):
        return PlainTextResponse(
            "This run has no stored observations to judge against. It has to "
            "be run from the asset.", status_code=409)
    known = set(market_packs())
    chosen = [m for m in markets if m]
    unknown = [m for m in chosen if m not in known]
    if unknown:
        return PlainTextResponse(f"No market pack for: {', '.join(unknown)}.",
                                 status_code=400)
    fresh = store().add_run_markets(run.id, chosen)
    if not fresh:
        # everything asked for was already judged: say so rather than
        # spending a model call to reach the same verdict twice
        return RedirectResponse(f"/runs/{run.id}", status_code=303)
    duration = asset_duration(run) or MAX_DURATION_S
    threading.Thread(target=_add_analysis_job, args=(run.id, fresh, duration),
                     name=f"analysis-{run.id}", daemon=True).start()
    return RedirectResponse(f"/runs/{run.id}", status_code=303)

# -- the launch board --

@app.get("/runs/{run_id}", response_class=HTMLResponse)
def launch_board(request: Request, run_id: str):
    run = _run_or_404(run_id)
    states = market_states(run)
    tiles = [{"market": m, "state": tile_state(s), **s,
              "pack": market_packs().get(m)} for m, s in states.items()]
    tiles.sort(key=lambda t: (TILE_ORDER.get(t["state"], 9), t["market"]))
    # The picker again, minus what this run has already been judged against:
    # a second pass exists to add jurisdictions, not to re-judge one.
    covered = set(run.markets)
    more = []
    for group in pack_groups():
        families = []
        for fam in group["families"]:
            left = [pk for pk in fam["packs"] if pk.market not in covered]
            if left:
                families.append(dict(fam, packs=left))
        if families:
            more.append(dict(group, families=families,
                             count=sum(len(f["packs"]) for f in families)))
    return _page(request, "launch_board.html", run=run, tiles=tiles,
                 more=more, has_poster=poster_available(run),
                 stills=board_stills(run, asset_duration(run)),
                 can_add=bool(store().observations(run.id)),
                 overall=overall(states), progress=run_progress(run, states),
                 embeds=embeds(run),
                 duration=asset_duration(run), published=published(run),
                 changes=len(store().changes(run.id)), screen="board")

_DIMENSION_BLURB = {
    "alcohol_tobacco_drugs": "drink, smoke and substances on screen",
    "religious_symbols_practices": "faith, ritual and sacred imagery",
    "modesty_dress_body": "how much skin a market allows",
    "gesture_body_language": "a gesture that means something else here",
    "food_and_animals": "what may be eaten, and what may not be shown",
    "gender_portrayal": "roles, stereotypes and objectification",
    "sexual_orientation_gender_id": "who may be shown together",
    "children_and_minors": "advertising to, and with, children",
    "national_symbols_politics": "flags, anthems, leaders, borders",
    "health_claims_pharma": "medicinal and nutritional promises",
    "gambling_and_finance": "betting, credit and financial promotion",
    "violence_and_weapons": "force, threat and weaponry",
    "language_profanity_idiom": "words that do not travel",
    "humour_irony_satire": "jokes that land differently",
    "superstition_number_colour": "numbers, colours and omens",
    "photosensitivity_sensory": "flashing, strobing and sensory risk",
    "text_legibility": "on-screen text, language and readability",
    "comparative_claims": "superiority, firsts and head-to-heads",
}


@app.get("/agent", response_class=HTMLResponse)
def agent_mode(request: Request, run: str = ""):
    """Agent mode: the operator types on the left, the console answers on
    the right.

    Same system, different front door. Studio mode is the one you click
    through; this one hands the wheel to a Vertex AI agent whose tools are
    the console's own reads and actions, so anything it says can be checked
    by looking at what it opened beside it.
    """
    recent = store().recent_runs(8)
    current = run or (recent[0].id if recent else "")
    return _page(request, "agent.html", runs=recent, run_id=current,
                 budget_left=max(0.0, costs.DAILY_BUDGET_EUR - store().spent_today()),
                 screen="agent")


@app.post("/agent/ask")
async def agent_ask(message: str = Form(...), session: str = Form("default"),
                    run: str = Form("")):
    """One turn with the console's agent.

    Answers with what it said and what it opened, never with a rendered
    page: the browser decides where to put the view, and a failed turn is
    reported rather than swallowed, because an agent that silently answers
    nothing is worse than one that says it could not.
    """
    text = (message or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="say something")
    if len(text) > 2000:
        raise HTTPException(status_code=400, detail="that is too long for one turn")
    turn = await agentmode.ask(store(), text, session_id=session, run_id=run)
    # The agent answers in prose; the console speaks in chips. reply_html
    # renders the same sentence in the interface's own language -- a rule
    # id as its class chip, a market as its country chip, a dimension as
    # its taxonomy icon -- so an answer reads like part of the product
    # rather than like a transcript pasted into it. Plain `reply` is kept
    # so anything consuming this API is unaffected.
    known = market_packs()
    rules = {r.id: r.klass for pack in known.values() for r in pack.rules}
    return {
        "reply": turn.reply or ("" if not turn.error else ""),
        "reply_html": replyfmt.render(turn.reply or "", markets=set(known), rules=rules),
        "view": turn.view, "view_label": turn.view_label,
        "view_external": turn.view_external,
        "calls": turn.calls, "error": turn.error,
    }


@app.get("/library", response_class=HTMLResponse)
def library(request: Request):
    """What this system actually tests for, by category, across every level.

    One card per observation dimension, carrying every rule that references
    it. own_rules, not rules: inheritance means a global rule is present in
    every pack beneath it, and the library is about where a rule is written,
    not how many packs end up carrying it.
    """
    all_packs = market_packs()
    cards = []
    for dimension in sorted(packs.taxonomy()):
        entries = [
            {"rule": rule, "pack": pack}
            for pack in all_packs.values() for rule in pack.own_rules
            if rule.dimension == dimension
        ]
        entries.sort(key=lambda e: (_LEVEL_ORDER.index(e["pack"].level)
                                    if e["pack"].level in _LEVEL_ORDER else 9,
                                    -e["rule"].severity))
        cards.append({
            "dimension": dimension,
            "label": dimension.replace("_", " "),
            "blurb": _DIMENSION_BLURB.get(dimension, ""),
            "entries": entries,
            "count": len(entries),
            "markets": len({e["pack"].market for e in entries}),
            "classes": {k: sum(1 for e in entries if e["rule"].klass == k)
                        for k in ("legal", "policy", "offence")},
        })
    cards.sort(key=lambda c: -c["count"])
    total = sum(c["count"] for c in cards)
    return _page(request, "library.html", cards=cards, total=total, screen="library",
                 packs_total=len(all_packs))


@app.get("/runs", response_class=HTMLResponse)
def all_runs(request: Request):
    """The archive: every run this store holds, newest first.

    Home shows the last 12; this is the whole book. The 500 cap is a page
    weight guard, not pagination -- at demo scale the store never gets there,
    and when it someday does, this is the seam where real paging goes.
    """
    # No lane charts built here. Doing it inline meant one Loki round trip
    # per run, server-side, before a single byte of the page went out --
    # twenty runs took the archive past a two minute timeout. The cards
    # request /lanes.svg themselves, so the page paints immediately and a
    # slow Grafana costs a chart rather than the page.
    #

    # A judge came to read what this thing has already done, so they get
    # the archive. Someone who just walked in came to watch it happen to
    # their own ad, and twenty of someone else's runs is not a welcome --
    # it is a wall between them and the one thing they wanted to try. So
    # a visitor's archive holds their runs and nothing else, and fills up
    # as they use it.
    #
    # Anyone who has not been through a door -- a direct link, a judge who
    # bookmarked this page -- sees everything, which is the old behaviour.
    runs = store().recent_runs(500)
    mine = _mine(request)
    scoped = _role(request) == "visitor"
    if scoped:
        runs = [r for r in runs if r.id in set(mine)]
    rows = [{"run": run, "states": (st := market_states(run)),
             "groups": pill_groups(run, st),
             "gauge": clearance_gauge(st)}
            for run in runs]
    return _page(request, "runs.html", rows=rows, screen="runs", scoped=scoped,
                 packs_total=len(market_packs()), dims_total=len(packs.taxonomy()))

@app.get("/runs/{run_id}/timeline", response_class=HTMLResponse)
def timeline(request: Request, run_id: str):
    """Market x timecode: where the commercial goes wrong, per country.

    One lane per market, one segment per finding drawn at its span on the
    asset's own clock. Hovering a segment shows the evidence: the triggering
    frame (when its file is still on disk), the rule, the class, the severity
    and the rationale. Resolved findings stay on the chart in the cleared
    colour: "was wrong here, fixed" is half the story this page tells.
    """
    run = _run_or_404(run_id)
    duration = asset_duration(run) or MAX_DURATION_S
    db = store()
    observations = {o.id: o for o in db.observations(run.id)}
    live = {oid for oid, o in observations.items()
            if o.evidence_frame and Path(o.evidence_frame).is_file()}
    states = market_states(run)
    lanes = []
    for market in run.markets:
        segs = []
        for f in sorted(db.findings(run.id, market), key=lambda f: f.t_start):
            left = max(0.0, min(99.0, f.t_start / duration * 100))
            width = max(0.9, min(100.0 - left, (f.t_end - f.t_start) / duration * 100))
            segs.append({"finding": f,
                         "left": round(left, 2), "width": round(width, 2),
                         "flip": left > 55,
                         "frame": f.observation_id in live})
        lanes.append({"market": market, "tile": tile_state(states[market]),
                      "segs": segs})
    ticks = [{"t": t, "left": round(t / duration * 100, 2)}
             for t in range(0, int(duration) + 1, 5)]
    return _page(request, "timeline.html", run=run, lanes=lanes, ticks=ticks,
                 duration=duration, screen="timeline")

@app.get("/runs/{run_id}/frames", response_class=HTMLResponse)
def frame_board(request: Request, run_id: str):
    """Every frame the crew looked at, beside what it made of it.

    The analyst's evidence already carries all of this: the still it read,
    the neutral sentence it wrote, the dimension it filed it under, and the
    findings each market's adjudicator then hung off that sentence. Laid out
    in timecode order it is the clearest answer to "why did it say that",
    which is the question a brand asks first.
    """
    run = _run_or_404(run_id)
    db = store()
    findings = db.findings(run.id)
    by_observation: dict[str, list] = {}
    for finding in findings:
        by_observation.setdefault(finding.observation_id, []).append(finding)

    rows = []
    for obs in sorted(db.observations(run.id), key=lambda o: (o.t_start, o.id)):
        hits = sorted(by_observation.get(obs.id, []),
                      key=lambda f: (-f.severity, f.market))
        rows.append({
            "obs": obs,
            "frame": bool(obs.evidence_frame and Path(obs.evidence_frame).is_file()),
            "findings": hits,
            "markets": sorted({f.market for f in hits}),
        })
    return _page(request, "frame_board.html", run=run, rows=rows,
                 total=len(rows), flagged=sum(1 for r in rows if r["findings"]),
                 screen="frames")


def _ticker(run) -> dict | None:
    """The newest thing an agent said, for the line under the progress bar.

    The bar answers "how far", which on a four minute run barely moves for
    a minute at a time. This answers "what, right now", so the page is
    visibly alive between percentage points.
    """
    latest = store().latest_event(run.id)
    if latest is None:
        return None
    event_id, agent, message = latest
    return {"id": event_id, "agent": agent, "message": message}

@app.get("/runs/{run_id}/status")
def run_status(run_id: str):
    """The 2 second poll behind the tiles. Shape is the contract, keep it."""
    run = _run_or_404(run_id)
    states = market_states(run)
    return {
        "run": run.id,
        "done": run.status in ("done", "error"),
        "status": run.status,
        "overall": overall(states),
        "progress": run_progress(run, states),
        "ticker": _ticker(run),
        "markets": states,
    }

# -- the mission feed --

def generated_items(run) -> list[dict]:
    """Everything a model produced for this run, newest first.

    The mission feed answers "what is happening"; this answers "what came
    out of it". Both stills, the raw Veo clip, and for a bridge the two
    anchor frames -- which are the brief Veo was given, and therefore the
    only way to tell whether it invented something or was handed it.
    """
    directory = run_dir(run) / "changes"
    by_id = {f.id: f for f in store().findings(run.id)}
    items = []
    for change in store().changes(run.id):
        finding = by_id.get(change.finding_id)
        anchors = [(tag, f"{change.id}_anchor_{tag}.png") for tag in ("head", "tail")]
        items.append({
            "change": change,
            "finding": finding,
            "market": finding.market if finding else "",
            "before": _still_name(run_dir(run), change.before_frame),
            "after": _still_name(run_dir(run), change.after_frame),
            "generated": (directory / f"{change.id}_bridge.mp4").is_file()
                         or (directory / ".chg_bridge.mp4").is_file(),
            "anchors": [{"tag": tag, "file": name} for tag, name in anchors
                        if (directory / name).is_file()],
        })
    items.reverse()
    return items


@app.get("/runs/{run_id}/mission", response_class=HTMLResponse)
def mission_page(request: Request, run_id: str):
    """The feed as a page: the backlog server-rendered, the rest over SSE.

    The backlog is rendered rather than replayed through the stream so the
    page is complete with JavaScript off and so a long run does not open with
    an empty terminal while a thousand events replay. The live tail then
    resumes from the last id on the page, which is exactly what the
    Last-Event-ID header does after a dropped connection.
    """
    run = _run_or_404(run_id)
    rows = store().events_since(run.id, 0)
    events = [{"id": i, "ts": ts, "agent": agent, "message": message}
              for (i, ts, agent, message) in rows]
    # Consecutive events from the same agent are one stage of the run, so the
    # feed shows a stage per row with its own progress and folds the detail
    # away. A thirty-shot run is eight rows, not a hundred and twenty lines.
    groups: list[dict] = []
    for event in events:
        if groups and groups[-1]["agent"] == event["agent"]:
            groups[-1]["events"].append(event)
        else:
            groups.append({"agent": event["agent"], "events": [event]})
    for group in groups:
        group["count"] = len(group["events"])
        group["last"] = group["events"][-1]["message"]
        group["ts"] = group["events"][0]["ts"]
        group["errored"] = any("stage_error" in e["message"] for e in group["events"])
    made = generated_items(run)
    return _page(request, "mission_feed.html", run=run, events=events,
                 groups=groups, running=run.status not in ("done", "error"),
                 made=made,
                 last_id=events[-1]["id"] if events else 0, screen="mission")

@app.get("/runs/{run_id}/feed")
async def mission_stream(request: Request, run_id: str):
    """Server-sent events: every store event for this run, as it lands.

    Resumes from `Last-Event-ID` (the browser sends it automatically on
    reconnect) or `?after=`, so a dropped connection costs nothing. The
    generator polls the store off the event loop; it ends when the client
    goes away, never on its own, because a finished run still emits events
    when a Grafana alert wakes the Remediator an hour later.
    """
    db = store()
    if db.get_run(run_id) is None:
        raise HTTPException(status_code=404, detail=f"unknown run: {run_id}")

    raw = request.headers.get("last-event-id") or request.query_params.get("after") or "0"
    try:
        cursor = int(raw)
    except ValueError:
        cursor = 0

    async def stream():
        nonlocal cursor
        yield "retry: 3000\n\n"  # reconnect delay, and an immediate first byte
        quiet_since = time.monotonic()
        while True:
            if await request.is_disconnected():
                return
            rows = await asyncio.to_thread(db.events_since, run_id, cursor)
            for (event_id, ts, agent, message) in rows:
                cursor = event_id
                data = json.dumps({"id": event_id, "ts": ts, "agent": agent,
                                   "message": message, "clock": _clock(ts)})
                yield f"id: {event_id}\nevent: mission\ndata: {data}\n\n"
            if rows:
                quiet_since = time.monotonic()
            elif time.monotonic() - quiet_since >= SSE_HEARTBEAT_S:
                yield ": heartbeat\n\n"
                quiet_since = time.monotonic()
            await asyncio.sleep(SSE_POLL_S)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",  # nginx and Cloud Run buffer SSE without it
    })

# -- one market --

@app.get("/runs/{run_id}/markets/{market}", response_class=HTMLResponse)
def market_room(request: Request, run_id: str, market: str):
    """One market: the regulator, the statutes, and the guard's refusals.

    `market` is checked against this run's own market list, which is also
    what makes every path built from it below safe: it can only ever be a
    string the run itself recorded.
    """
    run = _run_or_404(run_id)
    if market not in run.markets:
        raise HTTPException(status_code=404,
                            detail=f"run {run_id} does not cover {market}")
    findings = sorted(store().findings(run.id, market),
                      key=lambda f: (-f.severity, f.t_start))
    states = market_states(run)
    # Observation ids whose evidence keyframe is still on disk: the template
    # draws a thumbnail only for these, so a pruned workdir degrades to the
    # text-only row rather than a broken image.
    evidence = {o.id for o in store().observations(run.id)
                if o.evidence_frame and Path(o.evidence_frame).is_file()}
    # What each fix would cost, priced against the day's remaining budget so
    # the operator sees the number before pressing anything.
    dimension_of = {o.id: o.dimension for o in store().observations(run.id)}
    spent = store().spent_today()
    duration = asset_duration(run) or MAX_DURATION_S
    scopes = {f.id: scope_mod.classify(f, findings, duration) for f in findings}
    fixes = {
        f.id: {
            "scope": scopes[f.id],
            "options": costs.options(max(0.0, f.t_end - f.t_start), spent,
                                     scopes[f.id], f.substitutable),
            "suggestions": costs.suggestions(dimension_of.get(f.observation_id, ""), f),
            "verdict": scope_mod.verdict(scopes[f.id], f.substitutable),
        }
        for f in findings if f.status == "open" and not f.remediation_blocked
        and f.remediable
    }
    return _page(request, "market_room.html", run=run, market=market,
                 evidence=evidence, fixes=fixes, scopes=scopes,
                 scope_text=scope_mod.DESCRIPTION,
                 budget_left=max(0.0, costs.DAILY_BUDGET_EUR - spent),
                 budget_total=costs.DAILY_BUDGET_EUR,
                 pack=market_packs().get(market),
                 findings=[f for f in findings if not f.remediation_blocked],
                 blocked=[f for f in findings if f.remediation_blocked],
                 state=states.get(market, {}), tile=tile_state(states[market]),
                 localized=(run_dir(run) / f"localized_{market}.mp4").exists(),
                 screen="market")

@app.post("/runs/{run_id}/findings/{finding_id}/remediate")
def remediate_now(run_id: str, finding_id: str, background: BackgroundTasks,
                  method: str = Form("auto"), intent: str = Form(""),
                  replacement: str = Form("")):
    """Remediate one finding by hand. The demo's affordance, not the trigger.

    The real trigger is a Grafana alert rule firing into POST /webhook/alert;
    this exists so the Cutting Room can be filled on demand while a judge is
    watching, and it enforces the same rules the webhook does.

    Answers, and nothing else:
      404  no such finding in this run, or it is not open (already
           remediating, already resolved). Same answer a forged id gets:
           this route never reveals whether an id it refuses exists.
      409  the guard blocked it, or its class makes it non-remediable. The
           Market Room already shows that finding and that reason in full, so
           saying "this one needs a human" leaks nothing and is the honest
           answer to a button the page deliberately draws as disabled.
    """
    run = _run_or_404(run_id)
    finding = next((f for f in store().findings(run.id) if f.id == finding_id), None)
    if finding is None or finding.status != "open":
        raise HTTPException(status_code=404, detail="no open finding with that id")
    if finding.remediation_blocked or not finding.remediable:
        raise HTTPException(
            status_code=409,
            detail=finding.blocked_reason or "this finding is not auto-remediable")
    # What the operator chose, priced and budget-checked before anything
    # runs. "auto" keeps the old behaviour: plan() picks the patch method
    # from the finding's dimension and nothing is billed beyond one edit.
    span = max(0.0, finding.t_end - finding.t_start)
    choice = (method or "auto").strip()
    want = (replacement or "").strip() or None
    how = (intent or "").strip() or None
    # Derived from the priced methods rather than restated here, because a
    # method the picker offers and the route rejects is a dead button, and
    # this list has been out of date before.
    if choice != "auto" and choice not in {m.key for m in costs.METHODS}:
        raise HTTPException(status_code=400, detail=f"unknown method: {choice}")
    if choice != "auto":
        # Scope is advice: the operator picked this knowing the caveat the
        # picker showed them. Only what cannot physically or financially
        # run is refused here.
        ok, why = costs.available(choice, span, store().spent_today())
        if not ok:
            raise HTTPException(status_code=409, detail=why)
    price = 0.0 if choice == "auto" else costs.estimate(choice, span)
    # The charge is NOT made here. A bridge edits both anchor frames and
    # checks them first, and if the edit did not actually fix the finding it
    # never calls Veo, so there is nothing to charge for. remediate_and_verify
    # records it at the moment Veo is called -- still before the generation
    # returns, because a bridge that dies halfway consumed it, and a budget
    # that only counts successes is not a budget.

    store().emit(run.id, "remediator",
                 f"console requested remediation: {finding.rule_id} "
                 f"({finding.market}) -> {finding.id}"
                 + (f" [{choice}"
                    + (f", {price:.2f} EUR" if price else "")
                    + (f", {how}" if how else "")
                    + (f', "{want}"' if want else "") + "]" if choice != "auto" else ""))
    # Flip the row before answering, not inside the background task: the
    # browser follows this redirect in milliseconds and would otherwise
    # re-render the same page it just left, which reads as a dead button.
    # Whatever happens next puts the status back: apply() sets remediating
    # itself, verify resolves or reopens, and every refusal below restores it.
    store().update_finding_status(finding.id, "remediating", run.id)
    background.add_task(remediate_and_verify, run.id, finding.id, finding.market,
                        method=choice, replacement=want, intent=how)
    return RedirectResponse(f"/runs/{run.id}/markets/{finding.market}",
                            status_code=303)

# -- the cutting room --

@app.get("/runs/{run_id}/cutting", response_class=HTMLResponse)
def cutting_room(request: Request, run_id: str):
    """Original against localized, and the change record behind every edit."""
    run = _run_or_404(run_id)
    directory = run_dir(run)
    by_id = {f.id: f for f in store().findings(run.id)}
    changes = []
    for change in store().changes(run.id):
        finding = by_id.get(change.finding_id)
        changes.append({
            "change": change,
            "finding": finding,
            "market": finding.market if finding else "",
            "before": _still_name(directory, change.before_frame),
            "after": _still_name(directory, change.after_frame),
            # only a bridge leaves generated footage behind
            "generated": (directory / "changes" / f"{change.id}_bridge.mp4").is_file(),
        })
    localized = [m for m in run.markets
                 if (directory / f"localized_{m}.mp4").exists()]
    # Both players in a pair open on the first second this market's master was
    # edited, rather than at 0:00 where an edited master and its original are
    # identical by definition. A media fragment does it without a line of
    # JavaScript, and the poster frame the browser paints is then the frame
    # the argument is about.
    starts = {}
    for item in changes:
        finding = item["finding"]
        if finding is not None and finding.market not in starts:
            starts[finding.market] = max(finding.t_start - 0.5, 0.0)
    return _page(request, "cutting_room.html", run=run, localized=localized,
                 changes=changes, starts=starts, screen="cutting")

def _still_name(directory: Path, frame_path: str) -> str:
    """The filename a still is served under, or "" if it is not there.

    A ChangeRecord stores an absolute-ish path from the process that wrote
    it. Only the name is ever put in a URL, and the route below resolves that
    name inside this run's changes/ directory, so a record naming a file
    somewhere else on disk simply does not render.
    """
    if not frame_path:
        return ""
    name = Path(frame_path).name
    return name if (directory / "changes" / name).is_file() else ""

# -- run artifacts --

def _within(root: Path, name: str) -> Path | None:
    """Resolve `name` inside `root`, or None if it escapes.

    The whole of this console's path safety. `name` is the only place a URL
    segment ever reaches the filesystem, so it is resolved and then checked
    against the resolved root: "../../.env", an absolute "/etc/passwd", a
    symlink pointing out of the run directory and a percent-encoded mixture
    of all three all land outside and all get None, which the callers turn
    into 404. Nothing here trusts that the router already normalised the
    path, because it does not: FastAPI hands `{name:path}` over with its
    percent-escapes decoded and its dot segments intact.
    """
    try:
        resolved = (root / name).resolve()
        root_resolved = root.resolve()
    except (OSError, ValueError, RuntimeError):
        return None
    if resolved == root_resolved or root_resolved not in resolved.parents:
        return None
    return resolved

@app.get("/runs/{run_id}/media/original")
def media_original(run_id: str):
    """The asset as uploaded. Path comes from the store, never from the URL."""
    run = _run_or_404(run_id)
    path = Path(run.asset_path)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="the original is not on this disk")
    return FileResponse(path, media_type="video/mp4",
                        filename=f"{run.id}_original{path.suffix}")

@app.get("/runs/{run_id}/media/localized/{market}")
def media_localized(run_id: str, market: str):
    """One market's localized master, once the Remediator has written one."""
    run = _run_or_404(run_id)
    if market not in run.markets:
        raise HTTPException(status_code=404, detail=f"run does not cover {market}")
    path = run_dir(run) / f"localized_{market}.mp4"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="no localized master yet")
    return FileResponse(path, media_type="video/mp4",
                        filename=f"{run.id}_localized_{market}.mp4")

@app.get("/runs/{run_id}/evidence/{observation_id}")
def evidence_frame(run_id: str, observation_id: str):
    """The keyframe that made the analyst write the observation a finding cites.

    The path served is the one the analyst itself recorded on the observation
    (store data written by this process, never a URL segment), so the only
    thing the caller controls is which of this run's observation ids to ask
    for. Missing observation, empty evidence_frame and a file that no longer
    exists all answer the same 404: the route never reveals which it was.
    """
    run = _run_or_404(run_id)
    obs = next((o for o in store().observations(run.id) if o.id == observation_id), None)
    if obs is None or not obs.evidence_frame:
        raise HTTPException(status_code=404, detail="no evidence frame")
    path = Path(obs.evidence_frame)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="no evidence frame")
    media_type = "image/jpeg" if path.suffix.lower() in (".jpg", ".jpeg") else "image/png"
    return FileResponse(path, media_type=media_type)

def poster_available(run) -> bool:
    """Is there a still to show for this run, without making one to find out?

    The poster route answers 404 when there is nothing, which is fine for
    an <img> in a list that can quietly drop itself. The board puts the
    still in its header, where a broken image is a hole in the page, so
    it asks first. Three cheap checks in the order they are likely: the
    cached file, the master, then a kept evidence frame.
    """
    if (run_dir(run) / "poster.jpg").is_file():
        return True
    if run.asset_path and Path(run.asset_path).is_file():
        return True
    return any(o.evidence_frame and Path(o.evidence_frame).is_file()
               for o in store().observations(run.id))


def board_stills(run, duration: float | None) -> list[float]:
    """Timecodes to sample the master at for the board's rotating still.

    One still is a coin flip. Commercials open on black, on a fade, on a
    logo card -- so the frame at one second is quite often nothing at all,
    and the board ends up showing a black rectangle as its only visual
    reference to the film.

    Five, spread across the middle, and the extremes deliberately left
    alone: the first and last few percent of a commercial are exactly
    where the black and the end card live.
    """
    if not duration or duration <= 0:
        return []
    if not (run.asset_path and Path(run.asset_path).is_file()):
        return []   # a pruned master still gets the single fallback poster
    return [round(duration * f, 2) for f in (0.08, 0.28, 0.48, 0.68, 0.88)]


@app.get("/runs/{run_id}/poster.jpg")
def run_poster(run_id: str, at: float = 1.0):
    """A small still of the asset, cached, for the run lists and the board.

    Written once per (run, timecode) and served from disk after that. It
    falls back to an evidence frame when the upload is gone -- a pruned
    workdir keeps work/**/frames, so a run whose master was cleaned up can
    still say what it was a picture of.
    """
    run = _run_or_404(run_id)
    # at=1.0 keeps the original filename, so every poster already on disk
    # (and in the archive's browser caches) stays valid.
    stem = "poster" if abs(at - 1.0) < 1e-6 else f"poster_{at:g}"
    cached = run_dir(run) / f"{stem}.jpg"
    if not cached.is_file():
        source = Path(run.asset_path)
        if not source.is_file():
            frames = [Path(o.evidence_frame) for o in store().observations(run.id)
                      if o.evidence_frame and Path(o.evidence_frame).is_file()]
            if not frames:
                raise HTTPException(status_code=404, detail="nothing to show for this run")
            source = frames[0]
        try:
            media.poster(source, cached, at=at)
        except Exception as exc:  # noqa: BLE001 -- a missing thumbnail is not a 500
            log.warning("poster failed for %s: %s", run.id, exc)
            raise HTTPException(status_code=404, detail="no poster") from exc
    return FileResponse(cached, media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=86400"})

@app.get("/runs/{run_id}/spark.svg")
def run_spark(run_id: str):
    """This run's severity profile, drawn from Grafana's own numbers.

    The card cannot hold an iframe -- Grafana Cloud answers with
    x-frame-options: deny + frame-ancestors 'none' -- and a rendered PNG is the wrong shape for
    something this small: fixed size, fixed theme, and a second round
    trip. So Mimir stays the source of truth and the drawing happens
    here, in the product's own hex, as inline SVG.

    Served as its own URL rather than inlined into the page so a list of
    seventeen runs paints immediately and the charts arrive after, and so
    one slow Grafana never holds up the archive.
    """
    run = _run_or_404(run_id)
    if run.t0 is None:
        raise HTTPException(status_code=404, detail="this run has no mapped clock")
    cached = run_dir(run) / "spark.svg"
    fresh = cached.is_file() and (time.time() - cached.stat().st_mtime) < 300
    if not fresh:
        duration = asset_duration(run) or MAX_DURATION_S
        asset = Path(run.asset_path).stem or run.asset_path
        try:
            from customs.grafana_ops import GrafanaOps
            with GrafanaOps(settings) as ops:
                series = ops.prom_window(
                    f'max(customs_risk{{asset="{asset}"}})',
                    run.t0, run.t0 + duration, 56)
        except Exception as exc:  # noqa: BLE001 -- a card without a chart is fine
            log.warning("spark failed for %s: %s", run.id, exc)
            series = []
        points = series[0]["points"] if series else []
        svg = spark.sparkline(points, width=280, height=44)
        if not svg:
            raise HTTPException(status_code=404, detail="no series for this run")
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_text(svg)
    return Response(content=cached.read_text(), media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=300"})

@app.get("/runs/{run_id}/markets/{market}/spark.svg")
def market_spark(run_id: str, market: str):
    """One market's severity profile across the film, from Mimir.

    The same split as the run card: Grafana owns the series, the tile
    draws it. A tile has to feel instant and follow the current style
    mode, which rules out both an iframe and a rendered image.
    """
    run = _run_or_404(run_id)
    if market not in run.markets:
        raise HTTPException(status_code=404, detail="market not on this run")
    if run.t0 is None:
        raise HTTPException(status_code=404, detail="this run has no mapped clock")
    safe = re.sub(r"[^A-Za-z0-9_-]", "", market)
    cached = run_dir(run) / "sparks" / f"{safe}.svg"
    fresh = cached.is_file() and (time.time() - cached.stat().st_mtime) < 300
    if not fresh:
        duration = asset_duration(run) or MAX_DURATION_S
        asset = Path(run.asset_path).stem or run.asset_path
        try:
            from customs.grafana_ops import GrafanaOps
            with GrafanaOps(settings) as ops:
                series = ops.prom_window(
                    f'max(customs_risk{{asset="{asset}",market="{safe}"}})',
                    run.t0, run.t0 + duration, 40)
        except Exception as exc:  # noqa: BLE001
            log.warning("market spark failed for %s/%s: %s", run.id, market, exc)
            series = []
        points = series[0]["points"] if series else []
        # A stat card, not a bare line. The number is what carries the
        # panel: a cleared market draws "0" against a flat baseline, which
        # reads as a result. The line alone drew nothing on those tiles.
        hits = [f for f in store().findings(run.id, market)]
        peak = int(max((f.severity for f in hits), default=0))
        if peak:
            value, label = str(peak), "PEAK SEVERITY"
        else:
            value, label = str(len(hits)), "FINDINGS"
        svg = spark.statcard(points, value=value, label=label,
                             colour=state_mod.colour_for_severity(peak),
                             width=260, height=76)
        if not svg:
            raise HTTPException(status_code=404, detail="no series for this market")
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_text(svg)
    return Response(content=cached.read_text(), media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=300"})

@app.get("/runs/{run_id}/lanes.svg")
def run_lanes(run_id: str, full: int = 0):
    """This run's problem lanes, as its own image.

    Built here rather than inline in the archive: each chart is a Loki
    query, and doing twenty of them before the page renders is what took
    /runs past a two minute timeout. As a separate URL the page paints
    at once, the browser fetches the charts lazily, and one slow Grafana
    costs a chart instead of the archive.
    """
    run = _run_or_404(run_id)
    cached = run_dir(run) / ("lanes_full.svg" if full else "lanes.svg")
    fresh = cached.is_file() and (time.time() - cached.stat().st_mtime) < 600
    if not fresh:
        svg = problem_lanes(run, compact=not full)
        if not svg:
            raise HTTPException(status_code=404, detail="nothing observed in this run")
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_text(svg)
    return Response(content=cached.read_text(), media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=600"})

@app.get("/runs/{run_id}/lanes.png")
def run_lanes_grafana(run_id: str):
    """The same lanes, drawn by Grafana itself rather than by us.

    This is the real `customs-lanes` state timeline: one lane per dimension
    over `max_over_time(... | unwrap max_severity)`, coloured by the
    thresholds in state.py, rendered inside Grafana with its own service
    account. Same data as the SVG, same palette, but the picture is
    Grafana's -- and the "open in Grafana" chip beside it lands on the
    identical panel.

    The card strip stays app-drawn on purpose: Grafana Cloud's renderer
    floors a panel at 1000x500, which is more than twice the height a
    card gives it. At board width there is no such problem.

    A dead renderer falls back to the SVG rather than to nothing, so a
    Grafana outage costs the chart its provenance, not its existence.
    """
    run = _run_or_404(run_id)
    cached = run_dir(run) / "lanes.png"
    fresh = cached.is_file() and (time.time() - cached.stat().st_mtime) < 600
    if not fresh:
        try:
            from customs.grafana_ops import GrafanaOps
            asset = Path(run.asset_path).stem or run.asset_path
            duration = asset_duration(run) or MAX_DURATION_S
            with GrafanaOps(settings) as ops:
                png = ops.render_png("customs-lanes", 1, run, duration,
                                     width=1200, height=420, theme="dark",
                                     variables={"asset": asset, "run": run.id})
        except Exception as exc:  # noqa: BLE001 -- the SVG has the same facts
            log.warning("grafana lanes render failed for %s: %s", run.id, exc)
            return RedirectResponse(f"/runs/{run.id}/lanes.svg?full=1",
                                    status_code=302)
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_bytes(png)
    return Response(content=cached.read_bytes(), media_type="image/png",
                    headers={"Cache-Control": "public, max-age=600"})


@app.get("/runs/{run_id}/evidence/{observation_id}/box")
def evidence_box(run_id: str, observation_id: str):
    """Where in its frame this observation's subject is, 0-1000 normalised.

    Answered from the store when the analyst recorded one, and located on
    demand when it did not -- every observation made before boxes existed
    still gets an overlay, and pays for it once.

    The rectangle is drawn by the browser over the image. It is never
    written into the PNG: that file is what a remediation edits and what
    Veo is anchored on, so a box burned into it would be treated as part
    of the picture and could end up in the commercial.
    """
    run = _run_or_404(run_id)
    obs = next((o for o in store().observations(run.id) if o.id == observation_id), None)
    if obs is None:
        raise HTTPException(status_code=404, detail="no such observation")
    box = list(getattr(obs, "box", None) or [])
    if box:
        return {"box": box, "cached": True}
    frame = Path(obs.evidence_frame or "")
    if not frame.is_file():
        return {"box": [], "cached": False, "why": "the frame is not on disk"}
    try:
        box = analyst.locate(frame.read_bytes(), obs.statement)
    except analyst.LocateFailed as exc:
        # Not written back: a failed call is not an answer, and caching it
        # would make this observation permanently unlocatable.
        log.warning("locate failed for %s: %s", obs.id, exc)
        return {"box": [], "cached": False, "why": "could not reach the model"}
    try:
        store().set_observation_box(run.id, obs.id, box)
    except ValueError:
        pass
    return {"box": box, "cached": False}

@app.get("/runs/{run_id}/stills/{filename:path}")
def still(run_id: str, filename: str):
    """A before/after still from runs/{run_id}/changes/, and nothing else."""
    run = _run_or_404(run_id)
    path = _within(run_dir(run) / "changes", filename)
    if path is None or path.suffix.lower() != ".png" or not path.is_file():
        raise HTTPException(status_code=404, detail="no such still")
    return FileResponse(path, media_type="image/png")
