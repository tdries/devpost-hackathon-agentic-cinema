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

from fastapi import (BackgroundTasks, FastAPI, File, Form, HTTPException,
                     Request, UploadFile)
from fastapi.responses import (FileResponse, HTMLResponse, PlainTextResponse,
                               RedirectResponse, StreamingResponse)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from customs import adjudicate, costs, packs, persist, pipeline, remediate, scope as scope_mod, verify
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
                         replacement: str | None = None) -> bool:
    try:
        return _remediate_and_verify(run_id, finding_id, market, workdir,
                                     method=method, replacement=replacement)
    finally:
        # the edit, the change record and the verifier verdict all just
        # landed in the store; put them somewhere a new revision can find
        store().emit(run_id, "remediator", persist.snapshot(settings.db_path))


def _remediate_and_verify(run_id: str, finding_id: str, market: str,
                          workdir=None, *, method: str = "auto",
                          replacement: str | None = None) -> bool:
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
            # What the violation's shape allows, checked before anything
            # runs. A centre crop over a baby suspended in the middle of the
            # frame is not a fix, it is a re-encode that the verifier then
            # has to reject: refusing here says so honestly and costs
            # nobody a cycle. See customs/scope.py.
            shape = scope_mod.classify(finding, db.findings(run_id),
                                       asset_duration(run) or 120.0)
            chosen = "bridge" if method == "bridge" else remediate.plan(finding, observation)
            allowed, why = scope_mod.allows(shape,
                                            "bridge" if chosen == "bridge" else "overlay",
                                            finding.substitutable)
            if not allowed:
                db.emit(run_id, "remediator",
                        f"{finding.rule_id} ({market}) -> not remediable at "
                        f"{shape} scope: {why}")
                return False
            db.emit(run_id, "remediator",
                    f"{finding.rule_id} ({market}) -> planned {chosen}")
            change = remediate.apply(run, finding, chosen, workdir, db,
                                     replacement=replacement)
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
        states[market] = {
            "clearance": adjudicate.clearance(findings) if decided else "pending",
            "findings": len(findings),
            "blocked": sum(1 for f in findings if f.remediation_blocked),
            "errored": market in errored,
        }
    return states

def tile_state(state: dict) -> str:
    """The one word a tile is drawn with. Errored beats every verdict."""
    return "error" if state["errored"] else state["clearance"]

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
    if run.t0 is None:
        return {"overview": overview,
                "timeline": f"{settings.grafana_public_timeline}?from=now-3h&to=now"}
    duration = asset_duration(run)
    start_ms = int((run.t0 - 5) * 1000)
    end_ms = int((run.t0 + duration + 5) * 1000)
    return {
        "overview": overview,
        "timeline": f"{settings.grafana_public_timeline}?from={start_ms}&to={end_ms}",
    }

# --- the instrument panel ------------------------------------------------
#
# The design spec pinned this as the most likely thing to break late, and it
# broke exactly there: "An iframe cannot carry a bearer token and Grafana
# Cloud has no anonymous access, so embedding is an auth problem." Public
# dashboards solved the auth half -- those URLs open with no login, and the
# board links to them -- but this stack answers every request, public
# dashboards included, with `Content-Security-Policy: frame-ancestors 'none'`
# (verified live, 2026-08-23). A browser refuses to frame that, and the page
# gets an empty box.
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

def _page(request: Request, name: str, **context):
    return templates.TemplateResponse(request, name, context)

# -- the front door --

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    """Upload form plus the runs already in the store, newest first."""
    db = store()
    recent = []
    for run in db.recent_runs(12):
        recent.append({"run": run, "states": market_states(run)})
    return _page(request, "home.html",
                 groups=pack_groups(),
                 recent=recent)

@app.post("/runs")
async def create_run(asset: UploadFile | None = File(None),
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
    return RedirectResponse(f"/runs/{run.id}", status_code=303)

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

# -- the launch board --

@app.get("/runs/{run_id}", response_class=HTMLResponse)
def launch_board(request: Request, run_id: str):
    run = _run_or_404(run_id)
    states = market_states(run)
    tiles = [{"market": m, "state": tile_state(s), **s,
              "pack": market_packs().get(m)} for m, s in states.items()]
    tiles.sort(key=lambda t: (TILE_ORDER.get(t["state"], 9), t["market"]))
    return _page(request, "launch_board.html", run=run, tiles=tiles,
                 overall=overall(states), embeds=embeds(run),
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
    return _page(request, "library.html", cards=cards, total=total,
                 packs_total=len(all_packs))


@app.get("/runs", response_class=HTMLResponse)
def all_runs(request: Request):
    """The archive: every run this store holds, newest first.

    Home shows the last 12; this is the whole book. The 500 cap is a page
    weight guard, not pagination -- at demo scale the store never gets there,
    and when it someday does, this is the seam where real paging goes.
    """
    rows = [{"run": run, "states": market_states(run)}
            for run in store().recent_runs(500)]
    return _page(request, "runs.html", rows=rows)

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
        "markets": states,
    }

# -- the mission feed --

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
    return _page(request, "mission_feed.html", run=run, events=events,
                 groups=groups, running=run.status not in ("done", "error"),
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
            "suggestions": costs.suggestions(dimension_of.get(f.observation_id, "")),
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
                  method: str = Form("auto"), replacement: str = Form("")):
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
    if choice not in ("auto", "overlay", "track", "bridge"):
        raise HTTPException(status_code=400, detail=f"unknown method: {choice}")
    if choice != "auto":
        finding_scope = scope_mod.classify(finding, store().findings(run.id),
                                           asset_duration(run) or MAX_DURATION_S)
        ok, why = scope_mod.allows(finding_scope, choice, finding.substitutable)
        if not ok:
            raise HTTPException(status_code=409, detail=why)
        ok, why = costs.available(choice, span, store().spent_today())
        if not ok:
            raise HTTPException(status_code=409, detail=why)
    price = 0.0 if choice == "auto" else costs.estimate(choice, span)
    if choice == "bridge":
        # Written down before the work starts: a bridge that dies halfway
        # still consumed the generation, and a budget that only counts
        # successes is not a budget.
        store().record_spend("bridge", price, run.id, finding.id)

    store().emit(run.id, "remediator",
                 f"console requested remediation: {finding.rule_id} "
                 f"({finding.market}) -> {finding.id}"
                 + (f" [{choice}"
                    + (f", {price:.2f} EUR" if price else "")
                    + (f', "{want}"' if want else "") + "]" if choice != "auto" else ""))
    background.add_task(remediate_and_verify, run.id, finding.id, finding.market,
                        method=choice, replacement=want)
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

@app.get("/runs/{run_id}/stills/{filename:path}")
def still(run_id: str, filename: str):
    """A before/after still from runs/{run_id}/changes/, and nothing else."""
    run = _run_or_404(run_id)
    path = _within(run_dir(run) / "changes", filename)
    if path is None or path.suffix.lower() != ".png" or not path.is_file():
        raise HTTPException(status_code=404, detail="no such still")
    return FileResponse(path, media_type="image/png")
