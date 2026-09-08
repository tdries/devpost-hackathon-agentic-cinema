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
import secrets
import shutil
import base64
import hashlib
import hmac
import threading
import time
import uuid
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

from fastapi import (BackgroundTasks, FastAPI, File, Form, HTTPException,
                     Query, Request, UploadFile)
from fastapi import Response
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               PlainTextResponse, RedirectResponse,
                               StreamingResponse)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from customs import (adjudicate, agentmode, analyst, certificate, costs,
                     grafana_map, grafana_ops, markers, media,
                     narrate, packs, replyfmt, persist, pipeline, remediate,
                     scope as scope_mod, search, spark, state as state_mod,
                     telemetry, tour, verify)
from customs.fetch import FetchError, fetch_youtube
from customs.config import is_withheld, settings, withheld_matcher
from customs.media import MediaError, probe_duration
from customs.store import Store

log = logging.getLogger("customs.app")

# docs_url/redoc_url/openapi_url off: this URL is public, and Swagger was
# publishing a try-it-now button for every POST the console has, including
# delete, remediate and the alert webhook. The routes are unchanged and the
# schema is still generated in code; it is simply not served.
@asynccontextmanager
async def lifespan(_app: FastAPI):
    """One question on the way up: does Omni's model still exist?

    In a thread, because a cold start should not wait on a metadata call,
    and never fatal: the probe's own rule is that only an unambiguous "no
    such model" takes the method away, so a container with no credentials
    (every test run, for one) boots exactly as it does today.
    """
    def ask() -> None:
        try:
            from customs.genai_client import probe_omni
            ok, why = probe_omni()
            if not ok:
                log.warning("omni preflight: %s", why)
        except Exception as exc:  # noqa: BLE001 -- a preflight cannot break boot
            log.debug("omni preflight skipped: %s", exc)

    threading.Thread(target=ask, name="omni-preflight", daemon=True).start()

    # And build the archive's first page before anybody asks for it. Cold,
    # that page is thirty-one runs of market states, findings and
    # observations -- about eight seconds -- and the first reader after a
    # deploy was paying all of it. Warm, it is a fifth of a second. Only
    # where there is a state dir, which is the tell for "this is the
    # deployed process" that the rest of this function already uses.
    def warm() -> None:
        try:
            if persist.state_dir() is None:
                return
            db = store()
            runs = db.recent_runs(12)
            for run in runs:
                _card_row(run, "light")
            edited_scenes()
            log.info("warmed %d card(s) and the edits page", len(runs))
        except Exception as exc:  # noqa: BLE001 -- a warm-up cannot break boot
            log.debug("warm-up skipped: %s", exc)

    threading.Thread(target=warm, name="warm-cards", daemon=True).start()

    # Per-call token and latency reporting, on for the deployed process and
    # off everywhere else: it fires on every model call, and a test suite
    # with a mocked Gemini would otherwise write a metric per mocked call
    # into the real tenant -- which is both a lie in somebody's dashboard
    # and, measured, a minute and a half of retry sleeps in the suite.
    #
    # Gated on the state dir, which is the same tell _sweep_orphaned_work
    # uses to answer "am I the deployed instance": a TestClient boots this
    # lifespan too, and a process-global switch flipped by one fixture
    # stays flipped for every test after it.
    if persist.state_dir() is not None:
        from customs.genai_client import report_usage
        report_usage(True)

    # No sweep of stale "remediating" findings here: _sweep_orphaned_work
    # above already does it at import, and gates itself on the state dir so
    # a test run cannot mutate a developer's local store. One mechanism.
    yield


app = FastAPI(title="Customs Launch Control",
              description="Ad clearance crew: console, mission feed, alert webhook",
              docs_url=None, redoc_url=None, openapi_url=None,
              lifespan=lifespan)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """The four headers a public page should not be without.

    No CSP: the console inlines its own SVG sprite and a few style
    attributes, and a policy written to allow those while claiming to stop
    anything would be decoration. The three below cost nothing and are
    true.
    """
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault(
        "Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response

_HERE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(_HERE / "templates"))
app.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")

# Cache-buster for the static assets, stamped into their URLs by the
# templates. Browsers heuristically cache /static without revalidating, so a
# deploy that changes a file would otherwise leave returning visitors on the
# old bytes -- old CSS when the style modes shipped, and an old landing
# video after its re-roll (the operator watched a cached cigarette while
# the server verifiably served the espresso). Every file in static/ counts
# now, not a hand-kept pair of names.
templates.env.globals["static_v"] = str(int(max(
    path.stat().st_mtime
    for path in (_HERE / "static").iterdir() if path.is_file())))

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


def _sweep_orphaned_work() -> None:
    """Statuses no thread can own after a boot, put back where they belong.

    This service is one container, and a deploy replaces it: any
    remediation thread dies with the old revision, and the finding it had
    moved to "remediating" stays there -- a "Working" row in the market
    room that never stops working. Same for a clearance run killed
    mid-pipeline: status "running", progress bar at 99, forever. At boot,
    by construction, nothing owns either status, so both are stale and
    both are swept honestly: the finding back to open (the alert stays
    up, which is true), the run to error with a stage error saying why.

    Gated on the state dir exactly like persist.restore above: without it
    there is no cross-boot store to sweep, and tests importing this module
    must never mutate a developer's local runs.
    """
    if persist.state_dir() is None:
        return
    db = store()
    for run in db.recent_runs(500):
        for f in db.findings(run.id):
            if f.status == "remediating":
                db.update_finding_status(f.id, "open", run_id=run.id)
                db.emit(run.id, "remediator",
                        f"stage_error: remediate: the service restarted while "
                        f"{f.id} was being remediated; finding back to open")
        if run.status in ("created", "running"):
            db.set_run_status(run.id, "error")
            db.emit(run.id, "pipeline",
                    "stage_error: run: the service restarted mid-run")


_sweep_orphaned_work()

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
            # The picker's word is law. It used to be "bridge or planner's
            # choice", which turned three explicit picks into two centre
            # crops and left per_frame unreachable from the console. Now:
            # bridge and per_frame run exactly as named; overlay forces the
            # single-frame freeze; track keeps the relight propagation.
            # plan() still decides WHICH edit (re-letter, swap, revoice) --
            # the picker chooses how it lands, not what it says.
            # The violation's shape is recorded, not enforced. It used to
            # refuse the job, and at concept scope it refused nearly every
            # one: the verifier is what decides whether an edit actually
            # worked, and it is better at that than a rule of thumb about
            # shape. A poor fit is written to the feed so the operator can
            # see it was expected. See customs/scope.py.
            shape = scope_mod.classify(finding, db.findings(run_id),
                                       asset_duration(run) or 120.0)
            # Evidence for the planner, and ONLY when nobody named a
            # method: the picker's word is law, and an operator who picks
            # overlay is entitled to see what a freeze does to the shot.
            # For "auto" the question is whether a patch is even the right
            # shape of answer -- does scope think it can reach this, does
            # the span move, and would Omni be allowed to run at all.
            # Three prop_swap fixes on the test ad passed the craft gate
            # and were reopened by the verifier ("FR-ALC-01 still fires")
            # because a wine bar is not a shot with a bottle in it.
            evidence: dict[str, object] = {}
            if method == "auto":
                span_s = max(0.0, finding.t_end - finding.t_start)
                reaches, _why = scope_mod.allows(shape, "overlay",
                                                 finding.substitutable)
                motion = 0.0
                try:
                    # This market's own master when one exists (an earlier
                    # fix may already have moved the span), else the source.
                    master = remediate.localized_master(run, market, db)
                    source = master if master.is_file() else Path(run.asset_path)
                    if source.is_file():
                        motion = media.motion_score(source, finding.t_start,
                                                    finding.t_end)
                except Exception as exc:  # noqa: BLE001 -- unmeasured is still
                    log.debug("motion score failed for %s: %s", finding.id, exc)
                evidence = {
                    "patch_reaches": reaches, "motion": motion,
                    "span": span_s,
                    "omni_ok": costs.available("omni", span_s,
                                               db.spent_today())[0],
                }
            technique = remediate.plan(finding, observation, **evidence)
            chosen, landing = {
                "bridge": ("bridge", None),
                "omni": ("omni", None),
                "per_frame": ("per_frame", None),
                "overlay": (technique, "freeze"),
            }.get(method, (technique, None))
            fits, why = scope_mod.allows(shape,
                                         "bridge" if chosen == "bridge" else "overlay",
                                         finding.substitutable)
            if not fits:
                db.emit(run_id, "remediator",
                        f"{finding.rule_id} ({market}) -> {shape} scope, "
                        f"running {chosen} anyway: {why}")
            db.emit(run_id, "remediator",
                    f"{finding.rule_id} ({market}) -> planned {chosen}"
                    + (" (single-frame freeze)" if landing == "freeze" else ""))
            span = max(0.0, finding.t_end - finding.t_start)

            def _charge(eur: float | None = None, *, _db=db, _run=run_id,
                        _fid=finding.id, _span=span, _method=chosen) -> None:
                _db.record_spend(_method,
                                 eur if eur is not None
                                 else costs.estimate(_method, _span),
                                 _run, _fid)
                # The ledger goes to Mimir too, so the day's budget is a
                # series with an alert rule on it rather than a number on
                # one page of the console. Never fatal: a fix that worked
                # is not undone by a telemetry hiccup.
                try:
                    telemetry.push_spend(_db.spent_today(), costs.DAILY_BUDGET_EUR)
                except Exception as exc:  # noqa: BLE001 -- the fix still stands
                    log.warning("spend telemetry failed: %s", exc)

            change = remediate.apply(
                run, finding, chosen, workdir, db,
                replacement=replacement, intent=intent,
                statement=observation.statement if observation else "",
                spend=_charge if chosen in ("bridge", "per_frame", "omni") else None,
                on_event=lambda agent, message: db.emit(run_id, agent, message),
                landing=landing)
            ok = verify.confirm(run, market, [change], db, workdir)
            if ok:
                # One asset, every market: the seconds this market just paid
                # to fix are the same seconds the other markets objected to,
                # so carry the confirmed picture into their cuts and let
                # their own packs rule on it. Costs no generation.
                verify.spread(run, market, [change], db, workdir)
            return ok
    except Exception as exc:  # noqa: BLE001 -- a background task has nobody to raise to
        log.exception("remediation of %s failed", finding_id)
        db.emit(run_id, "remediator", f"stage_error: remediate: {finding_id}: {exc!r}")
        return False

def _label(labels, key: str) -> str:
    value = labels.get(key)
    return value if isinstance(value, str) else ""


# Automatic remediation, held for the rest of a UTC day.
#
# Two of this system's three alert rules ask Grafana to wake the Remediator.
# The third asks it to stop: the loop that fixes a finding by generating
# video is the loop that can spend a day's budget while nobody is watching,
# so when customs_budget_remaining_eur crosses the floor the webhook stops
# starting paid work on its own. Findings still block, alerts still fire,
# and a person at the console can still spend what is left one fix at a
# time -- the pause is on the automatic path only, which is the one nobody
# is watching.
# ponytail: a module-level date, not a table. One instance is the deployment.
_REMEDIATION_PAUSED_DAY = ""


def _pause_remediation(reason: str) -> str:
    """Hold the automatic path for the rest of the UTC day."""
    global _REMEDIATION_PAUSED_DAY
    _REMEDIATION_PAUSED_DAY = time.strftime("%Y-%m-%d", time.gmtime())
    log.warning("automatic remediation paused for %s: %s",
                _REMEDIATION_PAUSED_DAY, reason)
    return _REMEDIATION_PAUSED_DAY


def remediation_paused() -> bool:
    """Is the automatic path held today?"""
    return _REMEDIATION_PAUSED_DAY == time.strftime("%Y-%m-%d", time.gmtime())

@app.post("/webhook/alert")
async def alert_webhook(request: Request, background: BackgroundTasks) -> dict:
    """Grafana alert contact point. Always 200, always fast.

    Reads alerts[].labels only. An alert whose labels name no open finding is
    logged and dropped: unknown asset, forged rule_id, an already-resolved
    finding and a resolved-status alert all land in that same branch, and
    none of them start any work.

    Authenticated by a shared token in the URL, because this route SPENDS:
    a forged alert naming a real open finding starts a paid generative fix
    and rewrites a localized master. Grafana cannot sign a request or send
    a header from a contact point, so the token rides in the query string
    that deploy.sh writes into the contact point, and the console's own
    doors are irrelevant here -- Grafana carries no cookie.

    An unset token means open, which is what the offline tests and a
    laptop want. Every deploy sets one.
    """
    want = settings.webhook_token
    if want and not secrets.compare_digest(request.query_params.get("key", ""), want):
        log.warning("alert webhook rejected: wrong or missing key")
        raise HTTPException(status_code=404, detail="no such endpoint")
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
        # The budget rule carries no asset: it is about the card, not a
        # commercial, and what it asks for is a stop rather than a fix.
        if _label(labels, "action") == "pause_remediation":
            _pause_remediation(_label(labels, "alertname") or "customs_budget_low")
            ignored += 1
            continue
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
        if remediation_paused():
            store().emit(run.id, "remediator",
                         f"alert received: {rule_id} {market} on {asset}, and "
                         f"held: the day's generation budget is nearly out, so "
                         f"automatic fixes are paused until midnight UTC. "
                         f"Remediate by hand if this one is worth it.")
            ignored += 1
            continue
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
    """The run, or a 404 -- including for a run this instance will not show.

    Twenty-five routes go through here (the board, the market rooms, every
    frame, poster, preview and localized master), which is why the
    withheld corpus is refused here and not in each of them. Nothing is
    deleted: the rows and the files stay, and config.WITHHELD_ASSETS is
    the whole of the policy.
    """
    run = store().get_run(run_id)
    if run is None or is_withheld(Path(run.asset_path).stem or run.asset_path):
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
    # Says out loud why five channels of one country read the same count:
    # a channel node IS its country's law until somebody writes its pack.
    "channel": ("a broadcaster's own acceptance rules on top of its "
                "country's, so a channel with no pack of its own carries "
                "exactly its country's count"),
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
        return {"pct": 100,
                "stage": "done" if run.status == "done" else "stopped on an error"}

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
    query = (f'{{app="customs", kind="observation"{withheld_matcher()}, '
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
    if not lanes and not compact:
        return ""
    # worst first, so the lane that blocks a market is the top line
    rows = [{"dimension": d, "events": sorted(e, key=lambda x: x["t"])}
            for d, e in sorted(lanes.items(),
                               key=lambda kv: -max((x["severity"] for x in kv[1]),
                                                   default=0))]
    if compact:
        # A card is not a page, and every card is the same card: the six
        # rows here are card_lanes' six, in card_lanes' order, so a reader
        # switching this drawing for the live panel beside it sees the same
        # categories in the same places. Rows the run never observed come
        # through with no events and are drawn faint.
        events_of = {row["dimension"]: row["events"] for row in rows}
        rows = [{"dimension": slot["dimension"],
                 "events": events_of.get(slot["dimension"], [])}
                for slot in card_lanes(run, market_states(run))]
        # A run with nothing dotted still gets its chart: six labelled,
        # faint, empty lanes, which is the honest picture of "watched for,
        # nothing seen" and means every card in the archive has a drawn
        # version to switch to. Returning "" here 404'd the image and left
        # those cards blank in drawn mode.
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


# Every card now carries both charts and boots the live one on approach,
# one at a time (customs.js). The old cap lived here because a page of
# lazy iframes all load at once; the queue replaced the cap.

# The one run a stranger should see first, and it has to carry the whole
# argument: 77 findings across seven dimensions and eight jurisdictions at
# all four rungs of the ladder, nine fixes that landed and were verified
# without anyone clicking (the alert fired, the Remediator woke, the
# Verifier re-observed), two findings the Guard refused to touch because
# the rule targets a protected characteristic, and one market that lost its
# judging pass to a 429 and wears an error tile rather than a green one it
# did not earn. Both doors point at it: the judge's archive pins it, the
# visitor's empty state and the launcher link it.
#
# It used to be a Chanel spot with a famous actor in it, which is a strange
# thing for a rights-clearance tool to lead with. This is the ad this
# project generated with Veo and loaded with its own documented landmines
# (docs/samples/landmines.yaml), so Google's tools made the film and then
# failed it.
# ponytail: hardcoded id, becomes a computed best-run when the archive churns
SHOWCASE_RUN = "run_959b162a0f25"

# The walkthrough the front page embeds, under the three fix clips. Its id
# rather than a URL, because the page builds three of them: the thumbnail,
# the iframe the first click writes, and the link out.
WALKTHROUGH_VIDEO = "zvr6GpmULr0"
# From the top. It opened ten seconds in to skip a title card, which is a
# guess about someone else's attention: a reader who pressed play asked for
# the film, not for the middle of it. Zero means no start parameter at all.
WALKTHROUGH_START = 0


def _showcase(db) -> str:
    """The showcase run's id, or "" when this store cannot show it.

    Withheld as well as missing: get_run answers for the whole store, and
    pinning "Start here" to a film every route refuses would be a link to
    a 404 on the busiest page here.
    """
    run = db.get_run(SHOWCASE_RUN)
    if run is None or is_withheld(Path(run.asset_path).stem or run.asset_path):
        return ""
    return SHOWCASE_RUN


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
    # A run that died before adjudication (ingest crashed on a corrupt or
    # audio-only asset) has zero findings in every market, and clearance([])
    # says "cleared" -- so without this line the deadest possible run drew
    # GO FOR LAUNCH with every tile green. If the run errored, any market
    # the adjudicators never answered for is an errored market.
    if run.status == "error":
        errored = errored | (set(run.markets) - judged)
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

def gtheme(request: Request | None) -> str:
    """Which Grafana theme this viewer's console is wearing.

    The console's own theme lives in localStorage, which the server cannot
    read, so the inline script in base.html mirrors it into a cookie for
    exactly this: a Grafana panel embedded in the light console has to come
    back light, and the same panel in Mission has to come back dark. Read
    server-side, the first paint is already right -- no reload, no second
    fetch, and no pair of near-identical images to keep in step.

    Absent or unreadable, the answer is light, which is the console's own
    default for the same reason.
    """
    cookie = request.cookies.get("customs-theme") if request else None
    return "dark" if cookie == "mission" else "light"


def embeds(run, theme: str = "light") -> dict[str, str]:
    """The two Grafana pages the board links out to, windowed for this run.

    Pure string building, no network call and no GrafanaOps: see
    config._PUBLIC_DASHBOARDS for why the share URLs are pinned instead of
    discovered, and grafana_ops.embed_url for the same rule applied to the
    per-panel form of these URLs.

    The two windows are deliberately different, because the two pages sit on
    different clocks (telemetry.py's module docstring is the reference):

      overview   t0-15m..now      status metrics are stamped at the real
                                  clock, and they move again every time a
                                  remediation resolves a finding, so the
                                  window has to reach the present. It is
                                  anchored at the run's own start rather
                                  than at now-6h, which is the same window
                                  on the day of a clearance and an empty
                                  panel on every day after it: judging runs
                                  for weeks, and a judge opening a
                                  three-week-old run was shown "No data"
                                  by a panel that had the data all along.
      timeline   t0..t0+duration  the risk series is written on the run's
                                  mapped clock, where wall time t0+n IS video
                                  second n, so this window is the ad's own
                                  timecode and nothing else.
    """
    # The embeddable viewer's own URLs, when it is deployed. Same dashboards,
    # same data (its datasources proxy through this very stack), but framed
    # rather than rendered -- so the panel in the console is the live one you
    # can hover, zoom and click. kiosk drops Grafana's chrome; the theme is
    # pinned dark to sit inside Mission.
    # Light, like the rest of the console now is. A theme-following iframe
    # would mean re-sourcing it on every toggle, and there is one theme to
    # follow.
    # Both overview URLs share it: the panels reduce with lastNotNull and
    # filter by asset, so a window that merely CONTAINS the run reads
    # correctly. A run that never started has no metrics under any window,
    # and 30 days is the honest outer bound of what the store still holds.
    since = (f"{int((run.t0 - 900) * 1000)}" if run.t0 is not None
             else "now-30d")
    overview_window = f"from={since}&to=now"

    viewer = {}
    if settings.grafana_viewer_url:
        base = settings.grafana_viewer_url
        asset_v = quote(Path(run.asset_path).stem or run.asset_path)
        common = f"var-asset={asset_v}&var-run={quote(run.id)}&kiosk&theme={theme}"
        if run.t0 is not None:
            span = asset_duration(run)
            # No padding either side: the console draws the axes for this
            # panel, so x% across the plot has to be x% through the film.
            lo, hi = int(run.t0 * 1000), int((run.t0 + span) * 1000)
            window = f"from={lo}&to={hi}"
        else:
            window = "from=now-3h&to=now"
        viewer = {
            # d-solo: the panel alone, no variable pickers and no time
            # picker, because the grid it sits inside is its chrome.
            "squares": f"{base}/d-solo/customs-grid/the-grid?panelId=1&{common}&{window}",
            # squares, to sit under the console's own grid
            "grid": f"{base}/d/customs-grid/the-grid?{common}&{window}",
            "lanes": f"{base}/d/customs-lanes/customs?{common}&{window}",
            "timeline": f"{base}/d/customs-timeline/customs?{common}&{window}",
            "overview": f"{base}/d/customs-overview/customs?{common}&{overview_window}",
        }

    overview = f"{settings.grafana_public_overview}?{overview_window}"
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
                "lanes": lanes, "viewer": viewer}
    duration = asset_duration(run)
    start_ms = int((run.t0 - 5) * 1000)
    end_ms = int((run.t0 + duration + 5) * 1000)
    return {
        "overview": overview,
        "timeline": f"{settings.grafana_public_timeline}?from={start_ms}&to={end_ms}",
        "lanes": f"{lanes}&from={start_ms}&to={end_ms}",
        "viewer": viewer,
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

@app.get("/runs/{run_id}/changes/{change_id}/span.mp4")
def change_span(run_id: str, change_id: str, side: str = "before",
                sound: int = 0):
    """One edit's own seconds, as a clip, from before it or after it.

    /edits compares an edit with two stills, which proves it happened and
    does not let anyone judge it: a hemline, a hand-off, a bottle being
    poured are motion. So each side of the pair is the span the finding
    names, cut from a master and kept beside the change record.

    before  the original master, always
    after   the model's own generated clip where there is one -- a bridge
            or an Omni rewrite -- and otherwise the same span cut out of
            that market's localized master, which is what a patch, a
            relight or a relettering actually produced.

    `sound=1` keeps the soundtrack. It is off by default because the video
    pairs play side by side and two soundtracks at once is neither, and on
    for a revoice, where the soundtrack IS the edit.

    Cut on demand and cached in the run's changes directory, which is
    mirrored, so the second reader pays nothing and a deploy does not lose
    it. The whole route 404s rather than guesses: no finding, no span; no
    master on this disk, no clip.
    """
    run = _run_or_404(run_id)
    if not re.fullmatch(r"chg_[0-9a-f]{6,32}", change_id):
        raise HTTPException(status_code=404, detail="no such change")
    if side not in ("before", "after"):
        raise HTTPException(status_code=404, detail="before or after")
    db = store()
    change = next((c for c in db.changes(run.id) if c.id == change_id), None)
    if change is None:
        raise HTTPException(status_code=404, detail="no such change")
    finding = next((f for f in db.findings(run.id)
                    if f.id == change.finding_id), None)
    if finding is None or finding.t_end <= finding.t_start:
        raise HTTPException(status_code=404, detail="that change has no span")
    changes = run_dir(run) / "changes"
    if side == "after":
        # the model's own output, unmodified, wherever it exists
        for name in (f"{change_id}_bridge.mp4", f"{change_id}_omni.mp4"):
            if (changes / name).is_file():
                return FileResponse(changes / name, media_type="video/mp4")
        source = run_dir(run) / f"localized_{finding.market}.mp4"
    else:
        source = Path(run.asset_path)
    if not source.is_file():
        raise HTTPException(status_code=404,
                            detail=f"no {side} master on this disk")
    cached = changes / (f"{change_id}_{side}_span"
                        + ("_snd" if sound else "") + ".mp4")
    try:
        if cached.is_file() and cached.stat().st_size:
            made = cached
        else:
            with _CUTTING:
                made = media.span_clip(source, cached, finding.t_start,
                                       finding.t_end, keep_audio=bool(sound))
    except Exception as exc:  # noqa: BLE001 -- a still poster is the fallback
        log.warning("span clip failed for %s %s: %s", change_id, side, exc)
        raise HTTPException(status_code=404, detail="could not cut that span") from exc
    return FileResponse(made, media_type="video/mp4",
                        headers={"Cache-Control": "public, max-age=86400"})


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
        # the Omni rewrite keeps its clip under its own suffix
        omni = changes / f"{change_id}_omni.mp4"
        if omni.is_file():
            return FileResponse(omni, media_type="video/mp4")
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
def grafana_png(request: Request, uid: str, run: str = "", theme: str = ""):
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
            # The dashboard knows its own window: chart_spec stamps time.from
            # on everything the agent builds. Rendering a hardcoded 24h here
            # was half of how the agent once said "62 findings" beside a
            # chart drawing 9 -- the window the agent asked for never
            # reached the renderer.
            window_ms = (now_ms - 24 * 3600 * 1000, now_ms)
            try:
                stored = ops._api_json("GET", f"/api/dashboards/uid/{uid}")
                span = re.fullmatch(r"now-(\d+)([mhd])",
                                    str(stored["dashboard"]["time"]["from"]))
                if span:
                    unit = {"m": 60_000, "h": 3_600_000, "d": 86_400_000}
                    window_ms = (now_ms - int(span.group(1)) * unit[span.group(2)],
                                 now_ms)
            except Exception:  # noqa: BLE001 -- no readable window, default holds
                pass
            # Wide: the pane this lands in is the right half of a desktop
            # window, so the render fills it rather than sitting as a small
            # card in the middle. The theme follows the console's, because a
            # light render on Mission's ground is a torch in a dark room.
            png = ops.render_png(uid, None, None, None, width=1600, height=900,
                                 theme=theme or gtheme(request),
                                 window_ms=window_ms)
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


def film_runs(run) -> list:
    """Every clearance of the same uploaded file, newest first.

    Two runs of ad.mp4 -- one judged against France, one against the Gulf --
    are two passes over one film, and the film is what the operator has. See
    asset_key for what "the same file" means here and what it costs.
    """
    key = asset_key(run)
    return [r for r in store().recent_runs(500) if asset_key(r) == key]


def other_pass_markets(run) -> dict:
    """Markets this film was judged against in ANOTHER pass, code -> run.

    A market appears once, from the newest run that judged it, and never
    from this one. recent_runs is newest first, so the first sighting wins.
    """
    mine = set(run.markets or [])
    found: dict[str, object] = {}
    for other in film_runs(run):
        if other.id == run.id:
            continue
        for code in other.markets or []:
            if code not in mine and code not in found:
                found[code] = other
    return found


def market_rows(run) -> list[dict]:
    """This film's markets, grouped into one row per jurisdiction level.

    A run can cover a global baseline, a continent, a dozen countries and
    twenty broadcasters at once, and a single strip of tabs makes that look
    like one flat list of codes. One row per level, labelled, says what you
    are actually looking at.

    The film's, not the run's. Upload ad.mp4 for France on Monday and for
    the Gulf on Thursday and you have two runs of one commercial, each
    knowing nothing about the other -- so the Gulf verdicts were invisible
    from the French run and there was no screen anywhere that answered "is
    this film cleared". The other pass's markets ride in the same strip,
    marked, each linking into the run that actually judged it.
    """
    all_packs = market_packs()
    # The tabs carry the verdict as a coloured underline. Without it the
    # only way to see that a channel is blocking was to scroll past the
    # tiles, which is the wrong way round: the tab strip is what you steer
    # by, and it was the one part of the board saying nothing.
    states = market_states(run)
    elsewhere = other_pass_markets(run)
    states_of: dict[str, dict] = {}
    for code, other in elsewhere.items():
        states_of.setdefault(other.id, market_states(other))

    def _level(code: str) -> str:
        return all_packs[code].level if code in all_packs else "national"

    rows = []
    for level in _LEVEL_ROWS:
        here = [{"code": c, "run": run.id, "elsewhere": None,
                 "state": tile_state(states[c]) if c in states else "pending"}
                for c in run.markets if _level(c) == level]
        there = []
        for code, other in elsewhere.items():
            if _level(code) != level:
                continue
            st = states_of.get(other.id) or {}
            there.append({"code": code, "run": other.id, "elsewhere": other.id,
                          "state": tile_state(st[code]) if code in st else "pending"})
        if here or there:
            rows.append({"level": level,
                         "markets": sorted(here, key=lambda m: m["code"])
                                    + sorted(there, key=lambda m: m["code"])})
    return rows


def market_ladder(run) -> list[dict]:
    """The whole ladder as ONE strip, with broadcasters folded.

    market_rows gives a row per level, which was four stacked lines of two
    or three codes each: a lot of vertical space on every run screen and a
    ragged left edge where the labels ran out of things to label. This is
    the same information as one line -- global, continental, national, and
    then a fold per country holding its channels -- because the ladder is
    a sequence and a reader steers by it.

    A fold rather than a row: BE-VRT, BE-VTM, BE-PLAY and BE-RTBF side by
    side read as four countries. One BE chip carrying their worst verdict
    reads as Belgium, and the four are one click away.
    """
    all_packs = market_packs()
    tiers = []
    for row in market_rows(run):
        if row["level"] != "channel":
            tiers.append(row)
            continue
        families: dict[str, list[dict]] = {}
        for pill in row["markets"]:
            pack = all_packs.get(pill["code"])
            parent = (pack.parent if pack and pack.parent else "") or "other"
            families.setdefault(parent, []).append(pill)
        folded = []
        for parent, children in families.items():
            worst = max(children,
                        key=lambda c: _STATE_RANK.get(c["state"], 0))
            folded.append({"code": parent, "state": worst["state"],
                           "run": children[0]["run"], "elsewhere": None,
                           "children": sorted(children,
                                              key=lambda c: c["code"])})
        tiers.append({"level": "channel",
                      "markets": sorted(folded, key=lambda f: f["code"])})
    return tiers


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
        if not items:
            continue
        markets = [{"code": c, "state": tile_state(st)} for c, st in sorted(items)]
        if label == "channel":
            # Collapsed under the country they hang off. A card that
            # cleared five Belgian broadcasters and three German ones
            # showed eight codes in a row and read as eight countries;
            # what a reader wants first is "Belgium, five channels, one of
            # them blocking", and the codes on request.
            markets = _by_country(markets, all_packs)
        # NOT "items": Jinja resolves group.items to dict.items, the
        # bound method, and iterating that is a TypeError at render.
        out.append({"label": label, "markets": markets,
                    "folded": label == "channel"})
    return out


# Worst wins when a country's channels disagree: a fold that reads
# "cleared" over a blocking broadcaster is worse than no fold at all.
_STATE_RANK = {"blocked": 3, "at_risk": 2, "noted": 1, "pending": 1,
               "cleared": 0}


def _by_country(markets: list[dict], all_packs: dict) -> list[dict]:
    """Channel pills grouped under their parent country, worst state first."""
    families: dict[str, list[dict]] = {}
    for pill in markets:
        pack = all_packs.get(pill["code"])
        parent = (pack.parent if pack and pack.parent else "") or "other"
        families.setdefault(parent, []).append(pill)
    out = []
    for parent, pills in families.items():
        worst = max(pills, key=lambda p: _STATE_RANK.get(p["state"], 0))
        out.append({"code": parent, "state": worst["state"],
                    "children": pills})
    return sorted(out, key=lambda f: (-_STATE_RANK.get(f["state"], 0), f["code"]))


def _page(request: Request, name: str, **context):
    # Every template can ask which Grafana theme this viewer is on, because
    # any of them may embed a panel. Set explicitly by a caller if it has a
    # reason to; otherwise read from the cookie base.html mirrors.
    context.setdefault("gtheme", gtheme(request))
    # Every screen inside a run gets the same four numbers, computed here
    # rather than in the eight routes that render one: cleared, blocked,
    # fixing, and what the fixes have cost. A person deep in the cutting
    # room could not see whether the run was finished without going back
    # to the board.
    run = context.get("run")
    if run is not None and "runstat" not in context:
        context["runstat"] = run_stat(run)
    return templates.TemplateResponse(request, name, context)


def run_stat(run) -> dict:
    """The four numbers every run screen carries in its header."""
    states = market_states(run)
    fixing = sum(1 for s in states.values() if s.get("working"))
    return {
        "cleared": sum(1 for s in states.values()
                       if s["clearance"] == "cleared" and not s["errored"]),
        "blocked": sum(1 for s in states.values()
                       if s["clearance"] == "blocked" or s["errored"]),
        "fixing": fixing,
        "markets": len(states),
        "eur": store().spent_on_run(run.id),
    }


def run_lifecycle(run) -> dict:
    """Where this run stands in the product's own story, and what to do next.

    The run screens are five flat tabs; nothing told a first-timer that the
    mission feed IS the processing stage or that the cutting room IS the
    result. This renders the lifecycle -- upload, processing, findings,
    decision, fix, verified -- as a strip on every run screen, the current
    stage lit, every stage linking to the screen that serves it, plus ONE
    state-driven next step so nobody finishes something and is left
    standing. Modern apps keep users oriented with exactly these two
    devices: a stepper for "where am I" and a status-driven call to
    action for "what now".
    """
    st = market_states(run)
    ov = overall(st)
    open_n = sum(v["open"] for v in st.values())
    working = sum(v["working"] for v in st.values())
    resolved = sum(v["resolved"] for v in st.values())
    findings_n = sum(v["findings"] for v in st.values())
    changed = bool(resolved) or working
    finished = run.status in ("done", "error")
    base = f"/runs/{run.id}"
    worst = next((m for m in ov["failing"] if not st[m]["errored"]), None)         or (ov["failing"][0] if ov["failing"] else None)

    def mark(done, current):
        return "done" if done else ("current" if current else "todo")

    stages = [
        {"key": "upload", "label": "upload", "href": None,
         "state": "done"},
        {"key": "processing", "label": "processing", "href": f"{base}/mission",
         "state": mark(finished, not finished)},
        {"key": "findings", "label": "findings", "href": f"{base}/frames",
         "state": mark(finished, False)},
        {"key": "decision", "label": "decision", "href": base,
         "state": mark(finished and not open_n, finished and open_n > 0 and not working)},
        {"key": "fix", "label": "fix",
         "href": f"{base}/markets/{worst}" if worst else base,
         "state": mark(changed and not open_n and not working, working > 0)},
        {"key": "verified", "label": "verified", "href": f"{base}/cutting",
         "state": mark(resolved > 0 and not open_n and not working, False)},
    ]

    if run.status == "error":
        cta = {"text": "The run stopped on an error. The mission feed has "
                       "the agents' own account of where.",
               "href": f"{base}/mission", "label": "Open the mission feed"}
    elif not finished:
        cta = {"text": "The crew is on it. The mission feed narrates every "
                       "step as it happens.",
               "href": f"{base}/mission", "label": "Watch the mission feed"}
    elif working:
        cta = {"text": "A fix is running. The market room shows it land, "
                       "and the verifier rules right after.",
               "href": f"{base}/markets/{worst}" if worst else base,
               "label": "Open the market room"}
    elif open_n and worst:
        cta = {"text": f"{open_n} finding{'' if open_n == 1 else 's'} still "
                       f"open. Every fix is priced in euro before you press.",
               "href": f"{base}/markets/{worst}",
               "label": f"Fix {worst}'s findings"}
    elif resolved:
        cta = {"text": "Fixed and verified. The cutting room has the "
                       "before and after, side by side.",
               "href": f"{base}/cutting", "label": "Open the cutting room"}
    elif findings_n:
        cta = {"text": "Cleared to air, with notes on record. The frame "
                       "board shows every scene and what was said about it.",
               "href": f"{base}/frames", "label": "Read the scenes"}
    else:
        cta = {"text": "Cleared everywhere, nothing on record. More markets "
                       "are cheap: they re-judge facts the run already has.",
               "href": base, "label": "See the board"}
    return {"stages": stages, "cta": cta}


templates.env.globals["market_rows"] = market_rows
templates.env.globals["market_ladder"] = market_ladder
templates.env.globals["run_lifecycle"] = run_lifecycle

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


# The role cookie is SIGNED. It used to be the bare word, which meant the
# door was decoration: `curl -b customs-role=judge` walked past it without
# the password, and the judge role is what lifts the spend ceiling. Anyone
# who read the public repo, or opened devtools once, could spend from the
# day's generation budget on a real card.
#
# HMAC over the value with a server-side secret, stdlib only. Not a session
# store: there is nothing to keep server-side, and one instance means one
# process. What it buys is exactly one thing -- the client can no longer
# write its own privileges.
def _sign(value: str) -> str:
    mac = hmac.new(settings.session_secret.encode(), value.encode(),
                   hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).decode().rstrip("=")[:27]


def _seal(value: str) -> str:
    """The cookie's contents: what it says, and proof this server said it."""
    return f"{value}.{_sign(value)}"


def _unseal(raw: str) -> str:
    """What the cookie says, or "" if this server did not write it."""
    value, _, signature = (raw or "").rpartition(".")
    if not value or not signature:
        return ""
    return value if hmac.compare_digest(signature, _sign(value)) else ""


def _role(request: Request) -> str:
    return _unseal(request.cookies.get(ROLE_COOKIE, ""))


def _mine(request: Request) -> list[str]:
    raw = _unseal(request.cookies.get(MINE_COOKIE, ""))
    return [r for r in (x.strip() for x in raw.split(",")) if r]


def _safe_next(path: str) -> str:
    """Where the door may send you afterwards: a path on this console.

    An absolute URL here would make the door an open redirect, and
    "//evil.example/x" is an absolute URL wearing a path's clothes.
    Anything else is dropped and the door falls back to its own landing.
    """
    p = (path or "").strip()
    if not p.startswith("/") or p.startswith("//") or "\\" in p:
        return ""
    return p


def _needs_word(request: Request, back: str) -> RedirectResponse | None:
    """The door, enforced. None when they are already through one.

    Reading is never gated: the landing page, the archive, every run,
    market room, statute, chart and frame stays open to anyone with the
    link. What is gated is everything that SPENDS or DESTROYS -- starting
    a clearance, judging more markets, remediating, asking the agent,
    deleting a run -- because those are models being called on a real card
    and rows that do not come back, and a submission link travels a great
    deal further than the people it was sent to.

    Having the word is not being someone: it is the speed bump, and the
    ceiling behind it (VISITOR_DAILY_EUR) is what actually bounds a
    stranger's spend.
    """
    if _role(request):
        return None
    return RedirectResponse(f"/enter/visitor?next={quote(back, safe='/')}",
                            status_code=303)


@app.get("/", response_class=HTMLResponse)
def landing(request: Request):
    """The front door: what this is, before what it does.

    Everything behind this page assumes you already know what ad
    clearance is and why fifteen markets disagree about a glass of wine.
    Someone arriving from a submission link does not, and the first thing
    they used to meet was an upload form asking for a master they do not
    have. So the form moved to /new and this says what the thing IS.
    """
    # packs_total and dims_total only: the band of four numbers that used
    # to sit under the doors is gone, and with it the run count -- which
    # was a read of every run in the store on every hit of the page most
    # likely to be hit.
    return _page(request, "landing.html", screen="landing",
                 roles=ROLES,
                 video_id=WALKTHROUGH_VIDEO, video_start=WALKTHROUGH_START,
                 packs_total=len(market_packs()),
                 tools_total=len(agentmode.TOOL_NAMES),
                 dims_total=len(packs.taxonomy()))


# A visitor's own ceiling, separate from the instance's. The generation
# budget is real money on a real card, and a link posted somewhere public
# should not be able to spend it: a visitor gets one euro of generation a
# day, counted over the runs they started.
VISITOR_DAILY_EUR = 10.0

# An agent turn calls a model with ten tools attached and was, until now,
# the one spending route with no ceiling of any kind: neither the visitor's
# nor the instance's. A loop against /agent/ask was unmetered Gemini spend
# on a real card.
#
# Turns are charged into the same ledger as generation, against a session id
# rather than against a run, because a turn belongs to nobody's run. The
# price is an estimate of a tool-calling turn and is deliberately not free:
# what matters is that the meter runs at all.
AGENT_TURN_EUR = 0.02
AGENT_DAILY_EUR = 10.0
SID_COOKIE = "customs-sid"


def _session_id(request: Request) -> str:
    """This browser's own id, signed, minted on first use.

    The visitor ceiling used to be summed over the run ids in a cookie the
    client writes, so clearing that cookie reset the meter to zero. A
    signed id cannot be edited into somebody else's, and while it can still
    be thrown away by clearing cookies, the instance ceiling behind it is
    what actually protects the card.
    """
    existing = _unseal(request.cookies.get(SID_COOKIE, ""))
    return existing or secrets.token_urlsafe(9)


def _agent_ledger(sid: str) -> str:
    return f"agent:{sid}"


def _agent_spent(sid: str) -> float:
    return store().spent_today_on([_agent_ledger(sid)])


def _visitor_spent(request: Request) -> float:
    return store().spent_today_on(_mine(request))


def _tour_context() -> dict:
    """The tour's slides and stops, with this instance's own numbers.

    Nothing here is typed into a slide by hand. A tour that claims 98
    jurisdictions while the packs say otherwise is a brochure, and the
    whole argument of this project is that the screens are not brochures.
    """
    db = store()
    packs_by_code = market_packs()
    inventory = grafana_map.totals()
    runs = db.recent_runs(500)
    findings = sum(len(db.findings(run.id)) for run in runs[:40])
    # The walk needs a finished, interesting clearance. That is what the
    # showcase run is; if this store does not hold it, the newest run with
    # findings will do, and if there is none the walk skips the run stops
    # rather than pointing at a 404.
    run_id = _showcase(db)
    if not run_id:
        run_id = next((r.id for r in runs if db.findings(r.id)), "")
    # And a market room with something in it. The walk used to name SA,
    # which is only the right answer on the instance that happened to run
    # it; what the stops actually need is a room holding a guard refusal
    # (the stop about what this system will not do) AND a finding the guard
    # left alone (the stop about what a finding is), so that is what is
    # preferred, then a room with a refusal, then the busiest room.
    rooms: dict[str, list] = {}
    for f in db.findings(run_id) if run_id else []:
        rooms.setdefault(f.market, []).append(f)

    def _room_rank(code: str) -> tuple:
        held = rooms[code]
        refused = any(f.remediation_blocked for f in held)
        plain = any(not f.remediation_blocked for f in held)
        return (refused and plain, refused, len(held), code)

    market = max(rooms, key=_room_rank, default="")
    stops = [dict(stop, path=stop["path"].replace("{run}", run_id)
                                         .replace("{market}", market))
             for stop in tour.WALK
             if (run_id or "{run}" not in stop["path"])
             and (market or "{market}" not in stop["path"])]
    slides = tour.slides(
        packs=len(packs_by_code),
        dimensions=len(packs.taxonomy()),
        rules=len({r.id for p in packs_by_code.values() for r in p.own_rules}),
        pairings=sum(len(p.rules) for p in packs_by_code.values()),
        dashboards=inventory["dashboards"], panels=inventory["panels"],
        series=inventory["series"], alert_rules=len(grafana_ops.ALERT_RULES),
        runs=len(runs), findings=findings, budget=costs.DAILY_BUDGET_EUR,
        stops=len(stops), tools=len(agentmode.TOOL_NAMES))
    return {"slides": slides, "stops": stops, "run_id": run_id}


@app.get("/tour", response_class=HTMLResponse)
def tour_deck(request: Request):
    """The onboarding tour: a carousel, and then a walk through the app.

    Open, like every other read here. The deck is server-rendered so the
    slides are in the page and a browser with no JavaScript still reads
    them top to bottom; the carousel, the autoplay and the walk are what
    the script adds.
    """
    context = _tour_context()
    return _page(request, "tour.html", screen="tour",
                 slides=context["slides"], stops=context["stops"],
                 run_id=context["run_id"],
                 tool_names=agentmode.TOOL_NAMES,
                 dims_all=sorted(packs.taxonomy()))


@app.get("/tour/walk.json")
def tour_walk():
    """The walk's stops, for the script that drives it across screens.

    Fetched once and kept for the session: each stop names a page, a
    data-tour hook on it and one sentence, and the script does the rest.
    """
    return {"stops": _tour_context()["stops"]}


@app.get("/enter/{role}", response_class=HTMLResponse)
def enter_form(request: Request, role: str, wrong: int = 0,
               next_: str = Query("", alias="next")):
    """The door.

    Not authentication: one shared password, carried in a cookie, and
    anyone who has the word is a judge. The visitor door asks for nothing
    at all -- it hands out its cookie and redirects -- because what
    actually bounds a stranger is the ceiling behind it, and a form asking
    for a word that everyone is given anyway is a speed bump with no
    speed behind it.
    """
    if role not in ROLES:
        raise HTTPException(status_code=404, detail=f"unknown door: {role}")
    # The visitor door has no word any more, on the owner's instruction:
    # it grants the cookie and gets out of the way. What actually bounds a
    # stranger is the ceiling behind it -- VISITOR_DAILY_EUR a day of
    # generation, which is the thing the word was ever really for. The
    # judge door keeps its word, because passing it lifts that ceiling.
    if role == "visitor":
        target = _safe_next(next_) or "/new"
        response = RedirectResponse(target, status_code=303)
        response.set_cookie("customs-role", _seal(role), max_age=60 * 60 * 24 * 30,
                            samesite="lax", httponly=False)
        return response
    # What the word is actually for, on both doors. The judge blurb used to
    # promise "the archive, the findings and every run this instance has
    # performed", which is a fair description of what is behind the door and
    # a misleading one about the door: all of that is open to anyone with
    # the link. The only thing either word governs is spending.
    if role == "visitor":
        target = _safe_next(next_) or "/new"
        response = RedirectResponse(target, status_code=303)
        response.set_cookie("customs-role", _seal(role), max_age=60 * 60 * 24 * 30,
                            samesite="lax", httponly=False)
        return response
    return _page(request, "enter.html", role=role, wrong=bool(wrong),
                 screen="landing", next=_safe_next(next_),
                 blurb=("Reading needs no password, and this door is not "
                        "how you get to it: the archive, every finding, "
                        "every statute and every chart are open, and so is "
                        "starting a clearance. The word here lifts the "
                        f"{VISITOR_DAILY_EUR:.2f} EUR a day generation cap "
                        "everyone else runs on, so a judge can watch the "
                        "fix loop run on the card."))


@app.post("/enter/{role}")
def enter(role: str, password: str = Form(""),
          next_: str = Form("", alias="next")):
    """Remember the door, and for a judge check the word first.

    A judge lands on the archive, because the work is already done and the
    interesting thing is reading it. A visitor lands on the upload form,
    because the interesting thing is watching it happen to their own ad.
    Unless they were on their way somewhere specific when the door stopped
    them, in which case they land there instead and finish what they
    started.
    """
    if role not in ROLES:
        raise HTTPException(status_code=404, detail=f"unknown door: {role}")
    back = _safe_next(next_)
    # The visitor door takes no word, on the owner's instruction, so a form
    # that still posts one is answered the same way the GET is: granted,
    # and sent where it was going. The ceiling behind it -- a visitor's
    # daily generation cap -- is what bounds a stranger now.
    if role == "visitor":
        response = RedirectResponse(back or "/new", status_code=303)
        response.set_cookie("customs-role", _seal(role), max_age=60 * 60 * 24 * 30,
                            samesite="lax", httponly=False)
        return response
    want = settings.judge_password
    # An unset password must mean the door is SHUT, not that any word opens
    # it. Comparing "" to "" is a match, so without this an instance with no
    # JUDGE_PASSWORD would hand the judge role, and the spend ceiling with
    # it, to an empty form.
    if not want or not secrets.compare_digest(password.strip(), want):
        again = f"/enter/{role}?wrong=1"
        if back:
            again += f"&next={quote(back, safe='/')}"
        return RedirectResponse(again, status_code=303)
    target = back or ("/runs" if role == "judge" else "/new")
    response = RedirectResponse(target, status_code=303)
    response.set_cookie("customs-role", _seal(role), max_age=60 * 60 * 24 * 30,
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
    # The form is the start of the one thing here that costs money, so it
    # asks for the word before it is filled in rather than after: being
    # bounced having already chosen markets and picked a file is a worse
    # way to meet a door.
    door = _needs_word(request, "/new")
    if door is not None:
        return door
    return _page(request, "home.html", groups=pack_groups(), screen="home",
                 showcase=_showcase(store()))

@app.post("/runs")
async def create_run(request: Request,
                     asset: UploadFile | None = File(None),
                     youtube_url: str = Form(""),
                     markets: list[str] = Form(default=[]),
                     pending: str = Form(""),
                     force: int = Form(0)):
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
    # Before the upload is read, let alone written: this is the expensive
    # door, and a stranger's file should not reach the disk.
    door = _needs_word(request, "/new")
    if door is not None:
        return door
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
    # The file this console is already holding, if this POST came from the
    # already-analysed page rather than from the form.
    held = _pending_asset(pending)
    if url and has_file:
        return PlainTextResponse(
            "Provide either an uploaded master or a YouTube link, not both.",
            status_code=400)
    if not url and not has_file and held is None:
        return PlainTextResponse(
            "Provide a master: an MP4 or a MOV, up to 30 MB.",
            status_code=400)

    # One directory per upload, keeping the file's own name inside it. The
    # name matters beyond tidiness: telemetry labels every metric and every
    # alert with the asset path's *stem*, so a uniquifying prefix on the
    # filename would follow this asset onto the dashboards and into the alert
    # labels as "a1b2c3_spring_launch". The directory carries the uniqueness
    # instead, and two uploads of the same filename still cannot collide.
    if held is not None:
        target, safe = held, held.name
    else:
        folder = uploads_dir() / uuid.uuid4().hex[:12]
        folder.mkdir(parents=True, exist_ok=True)
        target, safe = await _receive(asset, url, folder)
        if isinstance(target, PlainTextResponse):
            return target

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

    # Already analysed? An asset is its file stem here, and the observations
    # of a film describe the film rather than a jurisdiction -- so a second
    # clearance of the same master would pay for shot detection, keyframes
    # and a vision pass to arrive at facts this store already holds. Say so,
    # and offer the cheap door: judge the markets it has not seen yet,
    # starting at the adjudicator. The upload stays on disk so "again
    # anyway" is one click rather than another trip to the file picker.
    db = store()
    twin = next((r for r in db.recent_runs(500)
                 if asset_key(r) == (Path(safe).stem or safe)), None)
    if not force and twin is not None and db.observations(twin.id):
        judged = set(twin.markets)
        return _page(request, "already.html", screen="home", twin=twin,
                     asset_name=safe, duration=duration,
                     pending=f"{target.parent.name}/{target.name}",
                     chosen=chosen,
                     again=[m for m in chosen if m not in judged],
                     seen=[m for m in chosen if m in judged],
                     packs=market_packs())

    run = db.create_run(asset_path=str(target), markets=chosen)
    db.emit(run.id, "pipeline",
            f"console accepted {safe} ({duration:.1f}s) for "
            f"{', '.join(chosen)}")
    threading.Thread(target=_clearance_job, args=(run.id, str(target), chosen),
                     name=f"clearance-{run.id}", daemon=True).start()
    response = RedirectResponse(f"/runs/{run.id}", status_code=303)
    # Newest first, capped: a cookie is not a database and forty run ids
    # is already more archive than anyone builds in a sitting.
    remembered = [run.id] + [r for r in _mine(request) if r != run.id]
    response.set_cookie(MINE_COOKIE, _seal(",".join(remembered[:MINE_MAX])),
                        max_age=60 * 60 * 24 * 30, samesite="lax")
    return response


async def _receive(asset, url: str, folder: Path):
    """Get the master onto disk: a YouTube link or an upload stream.

    Returns (path, filename) or, when the master cannot be had, the
    PlainTextResponse to send instead -- lifted out of create_run so the
    handler reads as the sequence of decisions it makes rather than as two
    hundred lines with the interesting part at the bottom.
    """
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
            return PlainTextResponse(str(exc), status_code=400), ""
        return target, target.name
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
                status_code=400), ""
        return target, safe

_PENDING_REL = re.compile(r"[0-9a-f]{12}/[A-Za-z0-9._-]{1,80}")


def _pending_asset(rel: str) -> Path | None:
    """An upload this console is already holding, named by the browser.

    "Clear it again anyway" on the already-analysed page hands back the
    file that page was about rather than making somebody find it on their
    disk a second time. It arrives as a path in a form, so it is trusted
    the way any path from a form should be: matched against the exact
    shape this handler writes (the twelve-hex folder, one plain name),
    resolved, and required to sit inside the uploads directory and exist.
    Anything else is treated as no file at all.
    """
    rel = (rel or "").strip()
    if not _PENDING_REL.fullmatch(rel):
        return None
    root = uploads_dir().resolve()
    path = (root / rel).resolve()
    if root not in path.parents or not path.is_file():
        return None
    return path


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
def add_analysis(request: Request, run_id: str,
                 markets: list[str] = Form(default=[])):
    """Clear an existing run against more markets, reusing its observations.

    The screenshots, the transcript and the analyst's reading of them are
    already in the store and describe the film, not a jurisdiction. So this
    starts at the adjudicator: no download, no shot detection, no keyframes,
    no vision calls. See pipeline.judge_more.
    """
    door = _needs_word(request, f"/runs/{run_id}")
    if door is not None:
        return door
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
    # A market already on the run whose judging pass died is asked for
    # again. Vertex answers a share of any parallel fan-out with 429
    # RESOURCE_EXHAUSTED, and add_run_markets only ever reports what is
    # NEW -- so the one market that lost that race was unreachable from
    # the console forever, wearing an error tile on a run that could
    # otherwise be finished with one click.
    retries = [m for m in chosen
               if m not in fresh and m in pipeline.errored_markets(store(), run.id)]
    if retries:
        store().emit(run.id, "pipeline",
                     f"retrying {len(retries)} market(s) that errored: "
                     f"{', '.join(retries)}")
    fresh = fresh + retries
    if not fresh:
        # everything asked for was already judged: say so rather than
        # spending a model call to reach the same verdict twice
        return RedirectResponse(f"/runs/{run.id}", status_code=303)
    duration = asset_duration(run) or MAX_DURATION_S
    threading.Thread(target=_add_analysis_job, args=(run.id, fresh, duration),
                     name=f"analysis-{run.id}", daemon=True).start()
    return RedirectResponse(f"/runs/{run.id}", status_code=303)

def asset_key(run) -> str:
    """What makes two runs runs OF THE SAME FILM.

    The file's stem, which is already the asset's identity everywhere else
    in this system: telemetry labels every metric and every alert with it,
    the Grafana panels are keyed by it, and /runs/by-asset resolves it. It
    is a name, not a hash -- two different files called ad.mp4 read as one
    asset here. That is the same trade the dashboards already make, and the
    consequence is a card that groups two things or an "already analysed"
    page offering the wrong run, both of which cost one click to get past.
    """
    return Path(run.asset_path).stem or run.asset_path


@app.get("/runs/by-asset")
def run_by_asset(asset: str = ""):
    """The newest clearance of a given asset, by its file stem.

    What a click on the archive's live panel needs: its series are labelled
    by asset, because that is what the crew writes to Loki, and an asset can
    have been cleared more than once. Newest wins, which is what someone
    clicking a line on a chart of the last day means.
    """
    want = (asset or "").strip()
    if want:
        for run in store().recent_runs(500):
            if asset_key(run) == want:
                return RedirectResponse(f"/runs/{run.id}", status_code=303)
    return RedirectResponse("/runs", status_code=303)


@app.post("/runs/{run_id}/delete")
def delete_run(request: Request, run_id: str):
    """Remove a run: its store rows, its artifacts, its mirrored copy.

    Answers, and nothing else:
      404  no such run, or a visitor asking about someone else's -- the same
           answer either way, so this never reveals that a run exists.
      409  the showcase run, which the archive pins for judges and the
           landing page links to, or a run with work in flight. Deleting
           either would break a page or strand a paid edit half-done.

    The day's spend ledger is deliberately left alone: euros that were
    spent stay spent, and deleting a run is not a way to buy another
    generation.
    """
    door = _needs_word(request, f"/runs/{run_id}")
    if door is not None:
        return door
    run = _run_or_404(run_id)
    # Anyone who is not the judge may only delete their own. "Not the
    # judge" rather than "is the visitor" on purpose: an unrecognised word
    # in the role cookie is a stranger, and a stranger is held to the
    # narrower rule, not the wider one.
    if _role(request) != "judge" and run.id not in set(_mine(request)):
        raise HTTPException(status_code=404, detail="no such run")
    if run.id == SHOWCASE_RUN:
        raise HTTPException(
            status_code=409,
            detail="This is the run the archive pins and the front door links "
                   "to. Unpin it in app.py before deleting it.")
    db = store()
    if run.status in ("created", "running") or any(
            f.status == "remediating" for f in db.findings(run.id)):
        raise HTTPException(
            status_code=409,
            detail="Something is still running on this run. Wait for it to "
                   "finish, or the work is thrown away half-done.")
    directory = run_dir(run)
    db.delete_run(run.id)
    # The artifacts, then the mirror. Either can fail without leaving the
    # store inconsistent -- the run is already gone from it, and what is
    # left behind is unreferenced bytes rather than a half-deleted run.
    for target in (directory, (persist.state_dir() / directory.name
                               if persist.state_dir() else None)):
        if target is not None:
            shutil.rmtree(target, ignore_errors=True)
    persist.snapshot(settings.db_path)
    log.info("deleted run %s", run.id)
    return RedirectResponse("/runs", status_code=303)


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
                 other_passes=other_pass_markets(run),
                 more=more, has_poster=poster_available(run),
                 stills=board_stills(run, asset_duration(run)),
                 can_add=bool(store().observations(run.id)),
                 overall=overall(states), progress=run_progress(run, states),
                 embeds=embeds(run, gtheme(request)),
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
    # A turn with the agent is a model call, so this screen is behind the
    # door even though everything it shows you afterwards is readable
    # without one.
    door = _needs_word(request, "/agent")
    if door is not None:
        return door
    recent = store().recent_runs(8)
    current = run or (recent[0].id if recent else "")
    return _page(request, "agent.html", runs=recent, run_id=current,
                 tools=agentmode.TOOL_NAMES,
                 budget_left=max(0.0, costs.DAILY_BUDGET_EUR - store().spent_today()),
                 screen="agent")


@app.post("/agent/ask")
async def agent_ask(request: Request, response: Response,
                    message: str = Form(...),
                    session: str = Form("default"), run: str = Form("")):
    """One turn with the console's agent.

    Answers with what it said and what it opened, never with a rendered
    page: the browser decides where to put the view, and a failed turn is
    reported rather than swallowed, because an agent that silently answers
    nothing is worse than one that says it could not.
    """
    # The one place the door answers in JSON rather than a redirect: this
    # is fetched, and a 303 to an HTML page would arrive at the chat log
    # as a turn that failed for no stated reason. 403 with the shape the
    # page already knows how to draw says it in the log instead.
    if _needs_word(request, "/agent") is not None:
        return JSONResponse(status_code=403, content={
            "reply": "", "reply_html": "", "error":
            "Come in through a door first: the agent costs money per turn. "
            "Open /enter/visitor, which asks for nothing, and try again.",
            "view": "", "view_label": "", "view_external": False, "calls": []})

    # Two ceilings, both of which this route used to have none of. The
    # instance one is what protects the card: it is the only bound a
    # visitor cannot reset by clearing a cookie.
    sid = _session_id(request)

    def _remember(res):
        """Hand the session id back, so tomorrow's cap knows today's turns."""
        res.set_cookie(SID_COOKIE, _seal(sid), max_age=60 * 60 * 24 * 30,
                       samesite="lax", httponly=True)
        return res

    def refused(why: str):
        return _remember(JSONResponse(status_code=429, content={
            "reply": "", "reply_html": "", "error": why, "view": "",
            "view_label": "", "view_external": False, "calls": []}))

    if store().spent_today() + AGENT_TURN_EUR > costs.DAILY_BUDGET_EUR:
        return refused("Today's generation budget is spent, and an agent turn "
                       "costs money like everything else here. It resets at "
                       "midnight UTC.")
    if _role(request) != "judge" and _agent_spent(sid) >= AGENT_DAILY_EUR:
        return refused(f"You have used your {AGENT_DAILY_EUR:.2f} EUR of agent "
                       f"time today. It resets at midnight UTC.")
    store().record_spend("agent", AGENT_TURN_EUR, _agent_ledger(sid), "")

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
    _remember(response)
    return {
        "reply": turn.reply or ("" if not turn.error else ""),
        "reply_html": replyfmt.render(turn.reply or "", markets=set(known), rules=rules),
        "view": turn.view, "view_label": turn.view_label,
        "view_external": turn.view_external,
        "calls": turn.calls, "error": turn.error,
        # Where to go from here, from what this turn actually did. A chat
        # that answers and then shows an empty box makes the operator
        # invent the next question, and the invented one is usually the one
        # the console cannot do.
        "follow_ups": agentmode.follow_ups(turn),
    }


@app.get("/agent/progress")
def agent_progress(session: str = "default"):
    """What the turn now running has done so far, in words.

    Polled by agent mode while it waits. The agent records each tool call
    as it makes it, so this is the real sequence rather than a script: no
    turn in flight is an empty list, which is exactly what the page needs
    to stop asking.
    """
    turn = agentmode.LIVE.get(session)
    if turn is None:
        return {"running": False, "phases": []}
    return {"running": True,
            "phases": [agentmode.phrase(call) for call in list(turn.calls)]}


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
                 packs_total=len(all_packs), showcase=_showcase(store()))


@app.get("/search")
def search_frames(request: Request, q: str = "", dimension: str = "",
                  market: str = "", flagged: str = "", days: int = 30,
                  limit: int = 4000, format: str = "", mode: str = "semantic"):
    """Find frames by what the analyst said about them, across every run.

    "Show me the frames with a rabbit in them" is not a screen anybody
    designed: it is a line filter over the captions already sitting in
    Loki, rendered as the frames themselves. Which makes it the answer to
    a question nobody anticipated, which is the only kind worth building a
    console around.

    Reading, so no door. It costs one Loki query, which is why it is a
    route somebody asks for rather than something a page does on load.

    format=json answers with the counts and the rows, for the agent: it
    reasons over the numbers and hands the same URL to the right-hand
    pane, so what it says and what you see are one query.
    """
    from customs.grafana_ops import GrafanaOps
    # An empty search is the page, not a query. Without this, opening the
    # Frame search tab from the nav paged the whole Loki corpus and rendered
    # every caption as a card: seven seconds and two megabytes, with 2,334
    # images, for a screen whose job at that moment is to show a text box.
    if not q.strip() and not dimension and not flagged:
        empty = {"query": "", "mode": "none", "routed": [], "pattern": "",
                 "total": 0, "scanned": 0, "capped": False, "films": 0,
                 "by_asset": {}, "by_dimension": {}, "by_market": {},
                 "flagged": 0, "hits": []}
        if format == "json":
            return JSONResponse(content=empty)
        return _page(request, "search.html", screen="search", result=empty,
                     summary="Ask for what is in the footage: a rabbit, somebody "
                             "smoking, a hemline above the knee. Every caption the "
                             "analyst ever wrote, across every run, read for meaning.",
                     q="", dimension="", market="", flagged="", days=days,
                     mode=mode, model=settings.model_text,
                     dimensions=sorted(packs.taxonomy()),
                     showcase=_showcase(store()))
    try:
        if q.strip() and mode != "literal":
            # The fast path: the index answers in milliseconds and the
            # model reads eighty captions rather than four thousand. It
            # self-heals a cold index a few hundred at a time, so the first
            # search after a deploy is slower once rather than wrong.
            db = store()
            from customs import vectors
            if vectors.size(db) == 0:
                vectors.backfill(db, limit=1200)
            result = search.indexed(db, q, dimension=dimension, market=market,
                                    flagged=flagged)
        else:
            with GrafanaOps(settings) as ops:
                result = search.frames(ops, q, dimension=dimension, market=market,
                                       flagged=flagged, days=max(1, min(days, 90)),
                                       limit=max(1, min(limit, 12000)),
                                       mode="literal" if mode == "literal" else "semantic")
    except search.SearchError as exc:
        if format == "json":
            return JSONResponse(status_code=400, content={"error": str(exc)})
        return PlainTextResponse(str(exc), status_code=400)
    except Exception as exc:  # noqa: BLE001 -- Grafana being down is not a 500 here
        detail = f"Loki did not answer: {exc}"
        if format == "json":
            return JSONResponse(status_code=502, content={"error": detail})
        return PlainTextResponse(detail, status_code=502)

    if format == "json":
        return JSONResponse(content=result)
    return _page(request, "search.html", screen="search", result=result,
                 summary=search.summary(result), q=q, dimension=dimension,
                 market=market, flagged=flagged, days=days, mode=mode,
                 showcase=_showcase(store()),
                 model=settings.model_text,
                 dimensions=sorted(packs.taxonomy()))


# The cross-run board's own numbers, read once and held briefly. Every
# panel on /insight is a live Grafana panel, but the icon axis beside two
# of them is drawn by this app, and an axis that does not agree with the
# bars it labels is worse than no axis at all -- so the order comes from
# the same store the panel reads, not from a guess.
_INSIGHT_TTL = 120.0
_insight_cache: dict[str, tuple[float, list]] = {}


def _ranked(query: str, label: str) -> list[dict]:
    """[{key, n}] for a `sum by (label)` LogQL query, biggest first.

    Biggest first, and ties by name, because that is the order the panels
    this axis labels are sorted into. Trusting the store's order was the
    bug: sort_desc gets the values right and says nothing about how a tie
    is broken, five executions of the hero's expression came back in five
    different arrangements of the seven films at severity 95, and the
    console runs its own execution to lay the axis out. The values agreed
    and the identities did not, which is the one way an axis is worse than
    no axis. The panels now sort themselves the same two ways (see
    `ordered()` in scripts/make_insight_dashboard.py), so both halves land
    on an order neither store promises.

    The name key is lowercased because Grafana's is: its sortBy compares
    strings case-insensitively, which puts BOND_JAMES before boro_Comet
    where a codepoint sort puts Cigars between them.
    # ponytail: two names that differ only in punctuation could still
    # disagree with Grafana's collation; a proper ICU key is the upgrade.

    A dead Grafana returns an empty list rather than raising: the page's
    panels are iframes that will show their own error, and the icon axis
    simply has nothing to label. One failure mode, on the panel that owns
    it, instead of a 502 on a page of fourteen other working panels.
    """
    hit = _insight_cache.get(query)
    if hit and time.time() - hit[0] < _INSIGHT_TTL:
        return hit[1]
    try:
        from customs.grafana_ops import GrafanaOps
        with GrafanaOps(settings) as ops:
            rows = ops.loki_instant(query)
    except Exception as exc:  # noqa: BLE001 -- the panels still render
        log.warning("insight ranking failed for %s: %s", label, exc)
        return []
    ranked = sorted(
        ({"key": r["labels"].get(label, ""), "n": int(r["value"])}
         for r in rows if r["labels"].get(label)),
        key=lambda r: (-r["n"], r["key"].lower()))
    _insight_cache[query] = (time.time(), ranked)
    return ranked


@app.get("/insight", response_class=HTMLResponse)
def insight(request: Request):
    """Every clearance this instance has performed, read across runs.

    The rest of the console answers questions about ONE commercial. This
    page answers the ones a clearance desk actually has -- which subject
    costs us the most, which market is hardest, which rule fires on
    everything, whether the citations hold up -- and it answers them out of
    the same two stores the crew wrote during those runs.

    The panels are live Grafana, embedded from customs-insight. What this
    app adds is the axis: the taxonomy icons and the market marks are the
    console's own, drawn down the side of a Grafana bar chart whose own
    labels are hidden, ordered by the counts read here. Grafana cannot draw
    this product's icons and this product cannot draw Grafana's data, so
    each does the half it is good at.
    """
    # Each of these is the panel's own expression, sort_desc included, and
    # each panel carries the same two sorts this function applies, so the
    # axis is not merely sorted the same way: it is the same answer.
    # The same matcher the panels carry, so the axis and the bars are still
    # answering one question. config.WITHHELD_ASSETS is why it exists.
    keep = withheld_matcher()
    dims = _ranked('sort_desc(sum by (dimension) (count_over_time({app="customs", '
                   f'kind="finding"{keep}}}[30d])))', "dimension")
    markets = _ranked('sort_desc(sum by (market) (count_over_time({app="customs", '
                      f'kind="finding"{keep}}}[30d])))', "market")
    # Which mark a market wears is a question about its level, not its
    # code: the sprite holds sixteen countries, and EU, GLOBAL and every
    # broadcaster channel are not among them. Same rule the run nav uses,
    # so the same market wears the same icon on both screens. A market with
    # no pack at all -- one whose pack was renamed since the run -- reads
    # as national and falls back to the generic mark in the template.
    packs_by_code = market_packs()
    for row in markets:
        pack = packs_by_code.get(row["key"])
        row["level"] = pack.level if pack else "national"

    # The hero's axis is the footage itself: one poster per film, under the
    # block that carries its worst severity. Grafana has never seen the
    # footage and this app cannot chart live, so each draws its half.
    #
    # A film in the stores whose run has since been deleted keeps its block
    # and loses its poster: the block is what the store says, and inventing
    # a still for it would be worse than an empty frame.
    films = _ranked('sort_desc(max by (asset) (max_over_time({app="customs", '
                    f'kind="finding"{keep}}} | json | unwrap severity [30d])))',
                    "asset")
    newest: dict[str, str] = {}
    for r in store().recent_runs(500):
        newest.setdefault(asset_key(r), r.id)
    for row in films:
        row["run"] = newest.get(row["key"], "")
        row["state"] = ("blocked" if row["n"] >= 70
                        else "noted" if row["n"] >= 40 else "cleared")
    base = settings.grafana_viewer_url
    theme = gtheme(request)
    board = solo = ""
    if base:
        common = f"kiosk&theme={theme}&from=now-30d&to=now"
        board = f"{base}/d/customs-insight/customs?{common}"
        solo = f"{base}/d-solo/customs-insight/customs?{common}&panelId="
    return _page(request, "insight.html", screen="insight",
                 showcase=_showcase(store()),
                 dims=dims, markets=markets, films=films,
                 board=board, solo=solo,
                 packs_total=len(market_packs()),
                 dims_total=len(packs.taxonomy()))


@app.get("/grafana", response_class=HTMLResponse)
def grafana_resources(request: Request):
    """The whole Grafana surface, on one page.

    The claim this project makes is that Grafana is upstream of the work
    rather than a report produced after it, and that claim was only ever
    told in fragments: a panel on the board, a chip in a market room, a
    paragraph in the README. This is the inventory behind it.

    Everything on it is read from the definitions the crew provisions
    from -- the dashboard JSON, the alert rules, the transport table --
    so the page cannot drift into describing a stack nobody has. No
    network call: a round trip per section would make the one page whose
    job is to be legible the slowest in the console.
    """
    return _page(request, "grafana.html", screen="grafana",
                 showcase=_showcase(store()),
                 totals=grafana_map.totals(),
                 stack=grafana_map.stack(),
                 datastores=grafana_map.DATASTORES,
                 dashboards=grafana_map.dashboards(),
                 metrics=grafana_map.SERIES,
                 streams=grafana_map.STREAMS,
                 annotations=grafana_map.ANNOTATIONS,
                 alerting=grafana_map.alerting(),
                 operations=grafana_map.operations(),
                 reads=grafana_map.READS)


PAGE_SIZE = 9


def _by_film(runs) -> dict[str, list]:
    """Every run grouped by the film it cleared, newest first in each."""
    grouped: dict[str, list] = {}
    for run in runs:  # recent_runs is newest first, so [0] is the newest
        grouped.setdefault(asset_key(run), []).append(run)
    return grouped


# How many lanes a card draws, always, whatever the run found.
#
# Cards with eleven icons beside cards with two read as two different
# products. Six is what fits under a thumbnail, so six it is on every card:
# the run's own dimensions first, worst first, and dimmed marks for the rest
# of the slots -- which are not filler, they are the categories this system
# watched for and did not see.
CARD_LANES = 6


def _card_panel(run, theme: str, lanes: list[dict]) -> str:
    """The squares panel for one card, pinned to the rows it draws beside.

    var-dim is a textbox on customs-grid, so this is the panel being told
    which dimensions it may show: exactly the ones with an icon next to
    them, in the order the icons are in.
    """
    url = (embeds(run, theme).get("viewer") or {}).get("squares", "")
    if not url:
        return ""
    # EVERY row the card draws, not just the ones with observations behind
    # them. Pinning the panel to the seen dimensions only is what put five
    # rows beside six icons: the greyed icon had nothing to line up with and
    # everything below the gap pointed at the wrong category. The unseen
    # ones have a kind="watched" line apiece, so the panel has a row to draw
    # for them and it comes out empty, which is the truth.
    dims = [row["dimension"] for row in lanes]
    return f"{url}&var-dim={quote('|'.join(dims))}"


def card_lanes(run, states: dict) -> list[dict]:
    """The six rows a card draws: [{dimension, seen}], display order.

    Chosen by severity and then DISPLAYED alphabetically, because the live
    panel beside these icons sorts its own rows by dimension name (see the
    sortBy on customs-grid). Selection is about importance; order is about
    two halves of one picture agreeing.

    Read from this run's own rows rather than from Loki: a page of cards
    cannot afford a query each, which is the whole reason the lane chart is
    a separate lazily-fetched URL in the first place.
    """
    db = store()
    worst: dict[str, int] = {}
    for finding in db.findings(run.id):
        dimension = telemetry._dimension_for(finding.market, finding.rule_id)
        if dimension and dimension != "none":
            worst[dimension] = max(worst.get(dimension, 0), finding.severity)
    observed = {o.dimension for o in db.observations(run.id)
                if o.dimension and o.dimension != "none"}
    ranked = sorted(observed, key=lambda d: (-worst.get(d, 0), d))
    seen = sorted(ranked[:CARD_LANES])
    # The slots nothing filled: taxonomy order, so the same absent category
    # lands in the same place on every card of the archive.
    spare = [d for d in sorted(packs.taxonomy())
             if d not in observed][:CARD_LANES - len(seen)]
    return ([{"dimension": d, "seen": True} for d in seen]
            + [{"dimension": d, "seen": False} for d in spare])


# The archive and My edits both walk every run in the store: market
# states, findings, observations, a disk check per change. That is a
# hundred SQLite queries and a lot of Python for one page, and ten readers
# asking at once on a single instance is that ten times over -- which is
# how the service came to answer 429 to everybody.
#
# So the expensive part of a FINISHED run is remembered, keyed on the
# store's own fingerprint (Store.stamp: four counts and the last event id),
# so a cached row is only ever served while nothing has been written. The
# half-minute is a safety net on top of that, not the guarantee. A run
# still in flight is never cached at all, because its whole point is that
# it changes while you watch.
_CARD_TTL = 30.0
_CARD_CACHE: dict[str, tuple[float, dict]] = {}
# One builder per row, not ten. Ten readers arriving together on a cold
# cache each built all thirty-one cards, which is the same work ten times
# and the reason the first public link felt broken; the first one through
# builds it and the rest read what it wrote.
_CARD_BUILD = threading.Lock()
_SCENES_TTL = 30.0
_SCENES_CACHE: dict[str, tuple[float, list]] = {}


def _card_row(run, theme: str) -> dict:
    """One archive card's expensive half, cached while the run is done."""
    states = market_states(run)
    busy = (run.status in ("created", "running")
            or any(v["working"] for v in states.values()))
    # The viewer's URL is part of the key: without it a row built while no
    # viewer was configured is served back to a page that has one, and the
    # card silently loses its live panel.
    key = (f"{settings.db_path}:{run.id}:{run.status}:{theme}:"
           f"{bool(settings.grafana_viewer_url)}:{store().stamp()}")
    now = time.time()
    if not busy:
        hit = _CARD_CACHE.get(key)
        if hit and now - hit[0] < _CARD_TTL:
            return hit[1]
    if busy:
        lanes = card_lanes(run, states)
        return {"run": run, "states": states,
                "groups": pill_groups(run, states), "busy": True,
                "live_lanes": _card_panel(run, theme, lanes),
                "dims": lanes, "gauge": clearance_gauge(states)}
    with _CARD_BUILD:
        hit = _CARD_CACHE.get(key)        # somebody may have built it while
        if hit:                           # this request waited for the lock
            return hit[1]
        lanes = card_lanes(run, states)
        row = {"run": run, "states": states,
               "groups": pill_groups(run, states), "busy": False,
               "live_lanes": _card_panel(run, theme, lanes),
               "dims": lanes, "gauge": clearance_gauge(states)}
        if len(_CARD_CACHE) > 400:
            _CARD_CACHE.clear()
        _CARD_CACHE[key] = (now, row)
        return row


def _run_rows(runs, by_asset: dict[str, list], offset: int = 0,
              theme: str = "light") -> list[dict]:
    """One row per film, ready for the card template.

    `offset` is the card's position in the WHOLE archive, not in this page:
    the live-panel cap has to stay global, or every page of a load-more
    would boot another six Grafanas.
    """
    return [dict(_card_row(run, theme),
                 older=by_asset.get(asset_key(run), [run])[1:])
            for i, run in enumerate(runs, start=offset)]



# ------------------------------------------------------------- archive sorting
#
# Newest first is the right default -- the archive is mostly "what did I just
# do" -- but it is the wrong question for "which film is giving us the most
# trouble" or "what have we actually re-rendered". Each of these reads a
# number the card already shows, so the order and the card never disagree.
SORTS: dict[str, dict] = {
    "newest":     {"label": "newest first",        "key": "recency", "reverse": True},
    "oldest":     {"label": "oldest first",        "key": "recency", "reverse": False},
    "open-most":  {"label": "most open findings",  "key": "open",    "reverse": True},
    "open-least": {"label": "fewest open findings", "key": "open",   "reverse": False},
    "edits-most": {"label": "most rendered edits", "key": "edits",   "reverse": True},
    "edits-least": {"label": "fewest rendered edits", "key": "edits", "reverse": False},
    "scenes-most": {"label": "most scenes",        "key": "scenes",  "reverse": True},
    "scenes-least": {"label": "fewest scenes",     "key": "scenes",  "reverse": False},
}
DEFAULT_SORT = "newest"


def sort_metrics(run) -> dict:
    """The three counts the archive can be ordered by, for one run.

    Counted from this run's own rows rather than from Loki: the archive
    draws a page of cards and cannot afford a query each, which is the same
    reason the lane charts are their own lazily-fetched URLs.
    """
    db = store()
    try:
        findings = db.findings(run.id)
        open_now = sum(1 for f in findings if f.status == "open")
        # A change record is only written once an edit produced frames the
        # verifier then ruled on, so counting records counts rendered edits:
        # a refused Omni and a fix the craft gate discarded never get here.
        edits = sum(1 for c in db.changes(run.id) if c.after_frame)
        scenes = len({o.shot_id for o in db.observations(run.id) if o.shot_id})
    except Exception:  # noqa: BLE001 -- an unsortable run still belongs on the page
        return {"open": 0, "edits": 0, "scenes": 0}
    return {"open": open_now, "edits": edits, "scenes": scenes}


def sort_rows(rows: list[dict], order: str) -> list[dict]:
    """Order the archive. `rows` arrives newest first, which is the default.

    "Newest" is the store's own order rather than a timestamp, because a run
    record does not carry one -- recent_runs returns them newest first and
    that is what the page has always meant by new. So the two date orders
    are that order and its reverse, and the counted orders fall back to it
    to break a tie, which keeps them stable between reloads.
    """
    spec = SORTS.get(order) or SORTS[DEFAULT_SORT]
    if spec["key"] == "recency":
        return rows if spec["reverse"] else list(reversed(rows))
    counted = [(i, r, sort_metrics(r["run"])) for i, r in enumerate(rows)]
    counted.sort(key=lambda t: (t[2][spec["key"]], -t[0]), reverse=spec["reverse"])
    return [r for _, r, _ in counted]


@app.get("/runs", response_class=HTMLResponse)
def all_runs(request: Request, all: int = 0, offset: int = 0,
             fragment: int = 0, sort: str = DEFAULT_SORT):
    """The archive: every film this store holds, newest first, nine at a time.

    Each card carries a poster, a lane chart and, for the first few, a live
    Grafana frame. Thirty of those on one paint is a page that takes seconds
    to settle, so the first nine come with the page and the rest arrive when
    somebody asks. `all=1` is the no-JavaScript way to the whole book, and
    `fragment=1` renders the cards alone for the load-more fetch.
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
    # Reading is open here, always: the archive, every finding and every
    # statute are the point of the thing. A visitor's own runs are floated
    # to the top of it rather than being the whole of it.
    #
    # This used to REPLACE the archive with the visitor's own runs, which
    # made a visitor with no runs yet -- or one whose cookie stopped being
    # readable, as happened the day the cookies were signed -- open the page
    # and find the entire archive apparently deleted. Nothing about the
    # spend ceilings depends on this list, so failing closed bought no
    # safety and cost the one thing the console promises.
    scoped = False
    if _role(request) == "visitor" and mine:
        first = set(mine)
        runs = ([r for r in runs if r.id in first]
                + [r for r in runs if r.id not in first])
    # One card per FILM, not per run. Clearing the same master three times
    # in an afternoon -- which is what happens while a market pack is being
    # written -- filled the archive with three identical thumbnails, three
    # identical filenames and three sets of lanes, and the newest one was
    # the only one anybody wanted. The newest is the card; the others are
    # dated links under it, so nothing is hidden and nothing is repeated.
    by_asset = _by_film(runs)
    newest = [older[0] for older in by_asset.values()]
    order = sort if sort in SORTS else DEFAULT_SORT
    rows = sort_rows(_run_rows(newest, by_asset, theme=gtheme(request)), order)
    # One live panel, not thirty-five. Every card carries its own charts as
    # SVG for a reason -- building them inline once took this page past a two
    # minute timeout -- and an iframe per card would be thirty-five Grafana
    # applications booting in one browser. So the archive gets a single
    # instance-wide panel, live, and the cards stay drawings.
    live_history = ""
    if settings.grafana_viewer_url and not scoped:
        live_history = (f"{settings.grafana_viewer_url}/d-solo/customs-history/"
                        f"customs?panelId=1&kiosk&theme={gtheme(request)}"
                        # 30 days, which is what the dashboard itself
                        # defaults to and what the log store keeps: a
                        # 7-day window on an archive whose newest run is
                        # ten days old is a panel that shows nothing about
                        # a page full of runs.
                        f"&from=now-30d&to=now")
    films = len(rows)
    if fragment:
        # the cards alone, in the same markup the first page used
        page = rows[offset:offset + PAGE_SIZE]
        return _page(request, "_runcards.html",
                     rows=_run_rows([r["run"] for r in page], by_asset,
                                    offset=offset, theme=gtheme(request)),
                     showcase=_showcase(store()))
    shown = films if all else min(PAGE_SIZE, films)
    return _page(request, "runs.html", rows=rows[:shown], screen="runs",
                 sorts=SORTS, sort=order, all=bool(all),
                 scoped=scoped, shown=shown, films=films, more=shown < films,
                 runs_total=len(runs),
                 showcase=_showcase(store()), live_history=live_history,
                 packs_total=len(market_packs()), dims_total=len(packs.taxonomy()))

@app.get("/runs/{run_id}/scene")
def scene_at(run_id: str, t: float = -1.0):
    """The scene playing at a moment, as a redirect to its frames.

    The other half of a click on a Grafana square. The panel can only
    navigate a URL and all it knows is the coordinate -- the dimension and
    the instant on the mapped clock -- so this turns the instant into the
    shot it falls inside and lands on that scene's card, where the frames,
    the analyst's sentences and the findings all are.
    """
    run = _run_or_404(run_id)
    tt = t
    if tt > 1e9:  # Grafana hands epoch milliseconds
        tt = tt / 1000.0 - (run.t0 or 0.0)
    shots = {}
    for obs in store().observations(run.id):
        sid = obs.shot_id or obs.id
        lo, hi = shots.get(sid, (obs.t_start, obs.t_end))
        shots[sid] = (min(lo, obs.t_start), max(hi, obs.t_end))
    hit = next((sid for sid, (lo, hi) in sorted(shots.items(), key=lambda kv: kv[1])
                if lo - 0.5 <= tt <= hi + 0.5), "")
    anchor = f"#sc-{hit}" if hit else ""
    return RedirectResponse(f"/runs/{run.id}/frames{anchor}", status_code=303)


@app.get("/launch/remediate")
def launch_remediate(request: Request, run: str, background: BackgroundTasks,
                     finding: str = "", dimension: str = "", t: float = -1.0,
                     method: str = "omni"):
    """A remediation launched by a CLICK on a visualization.

    Two callers: the console's scene-by-dimension matrix (which knows the
    finding id outright), and a data link on the crew's own Grafana lanes
    dashboard -- which can only navigate a URL, and which hands back the
    click's COORDINATE: y is the dimension (the series label), x is the
    mapped clock, where wall second t0+n IS film second n. That pair plus
    the run is enough to name the open finding under the click, which is
    the whole test: a Veo or Omni process, started from a Grafana panel.

    Same guards as the console button: the finding must be open, the guard
    must not have blocked it, and the method must be affordable today.
    Lands on the market room's scene section, where the work sparkles.
    """
    # A GET that spends: this is the one route where a click somewhere else
    # entirely -- a data link on a Grafana panel -- starts a paid edit. The
    # door carries the whole click back with it, query and all, so saying
    # the word finishes the launch the panel asked for rather than dropping
    # the operator on a board and making them find the square again.
    here = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    door = _needs_word(request, here)
    if door is not None:
        return door
    rec = _run_or_404(run)
    db = store()
    if method not in {m.key for m in costs.METHODS}:
        raise HTTPException(status_code=400, detail=f"unknown method: {method}")
    obs_by_id = {o.id: o for o in db.observations(rec.id)}
    if finding:
        target = next((f for f in db.findings(rec.id)
                       if f.id == finding and f.status == "open"), None)
    else:
        tt = t
        if tt > 1e9:  # Grafana hands epoch milliseconds on the mapped clock
            tt = tt / 1000.0 - (rec.t0 or 0.0)
        cands = [
            f for f in db.findings(rec.id)
            if f.status == "open" and f.remediable and not f.remediation_blocked
            and (obs := obs_by_id.get(f.observation_id)) is not None
            and obs.dimension == dimension
            and (tt < 0 or f.t_start - 0.75 <= tt <= f.t_end + 0.75)
        ]
        target = max(cands, key=lambda f: f.severity, default=None)
    if target is None or target.remediation_blocked or not target.remediable:
        raise HTTPException(status_code=404,
                            detail="no open, remediable finding at that coordinate")
    span = max(0.0, target.t_end - target.t_start)
    # The same ceiling every other spending route applies. This one is a GET,
    # so it was also the one path a link on another site could walk a
    # visitor's cookie into a paid render.
    if _role(request) != "judge":
        spent = _visitor_spent(request)
        if spent >= VISITOR_DAILY_EUR:
            raise HTTPException(
                status_code=429,
                detail=(f"You have used your {VISITOR_DAILY_EUR:.2f} EUR of "
                        f"generation for today ({spent:.2f} EUR)."))
    ok, why = costs.available(method, span, db.spent_today())
    if not ok:
        raise HTTPException(status_code=409, detail=why)
    db.emit(rec.id, "remediator",
            f"viz click requested remediation: {target.rule_id} "
            f"({target.market}) -> {target.id} "
            f"[{method}, {costs.estimate(method, span):.2f} EUR]")
    background.add_task(remediate_and_verify, rec.id, target.id, target.market,
                        method=method)
    shot = getattr(target, "shot_id", "") or         (obs_by_id[target.observation_id].shot_id
         if target.observation_id in obs_by_id else "")
    anchor = f"#mk-{shot}" if shot else ""
    return RedirectResponse(
        f"/runs/{rec.id}/markets/{target.market}{anchor}", status_code=303)


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

    # The matrix: occurrence types down, scenes across, one cell per pair.
    # A cell with an open finding IS a launch button -- the same workflow a
    # Grafana data link on the lanes dashboard fires by coordinate.
    scenes = []
    scene_map: dict[str, dict] = {}
    for obs in sorted(observations.values(), key=lambda o: (o.t_start, o.id)):
        sid = obs.shot_id or obs.id
        sc = scene_map.get(sid)
        if sc is None:
            sc = scene_map[sid] = {"shot_id": sid, "t_start": obs.t_start,
                                   "t_end": obs.t_end, "hero": None}
            scenes.append(sc)
        sc["t_start"] = min(sc["t_start"], obs.t_start)
        sc["t_end"] = max(sc["t_end"], obs.t_end)
        if sc["hero"] is None and obs.id in live:
            sc["hero"] = obs.id
    scenes.sort(key=lambda sc: sc["t_start"])
    # A scene's column is as wide as the scene is long. Even columns could
    # never line up with Grafana's squares, which sit on the clock -- and a
    # ten second scene deserves more of the eye than a two second one.
    for i, sc in enumerate(scenes):
        nxt = scenes[i + 1]["t_start"] if i + 1 < len(scenes) else duration
        sc["span"] = max(0.4, min(nxt, duration) - sc["t_start"])
    total_span = sum(sc["span"] for sc in scenes) or 1.0
    for sc in scenes:
        sc["weight"] = round(sc["span"] / total_span * 1000)
    order = {sc["shot_id"]: i for i, sc in enumerate(scenes)}

    all_findings = db.findings(run.id)
    dims_present = []
    cells: dict[tuple[str, str], dict] = {}
    for obs in observations.values():
        if obs.dimension and obs.dimension != "none":
            if obs.dimension not in dims_present:
                dims_present.append(obs.dimension)
            key = (obs.dimension, obs.shot_id or obs.id)
            cells.setdefault(key, {"obs": 0, "open": 0, "resolved": 0,
                                   "working": 0, "worst": 0, "best": None})
            cells[key]["obs"] += 1
    for f in all_findings:
        obs = observations.get(f.observation_id)
        if obs is None or not obs.dimension or obs.dimension == "none":
            continue
        cell = cells.setdefault(
            (obs.dimension, obs.shot_id or obs.id),
            {"obs": 0, "open": 0, "resolved": 0, "working": 0,
             "worst": 0, "best": None})
        cell["worst"] = max(cell["worst"], f.severity)
        if f.status == "resolved":
            cell["resolved"] += 1
        elif f.status == "remediating":
            cell["working"] += 1
        elif f.status == "open" and f.remediable and not f.remediation_blocked:
            cell["open"] += 1
            best = cell["best"]
            if best is None or f.severity > best.severity:
                cell["best"] = f
    dims_present.sort()
    matrix = []
    for dim in dims_present:
        row = []
        for sc in scenes:
            cell = cells.get((dim, sc["shot_id"]))
            entry = None
            if cell:
                best = cell["best"]
                entry = {
                    "obs": cell["obs"], "open": cell["open"],
                    "resolved": cell["resolved"], "working": cell["working"],
                    "worst": cell["worst"],
                    "finding": best.id if best else "",
                    "market": best.market if best else "",
                    # The Gantt this grid replaced put the rule and the
                    # rationale in a hover card; a cell keeps them in its
                    # tooltip rather than losing them to the market room.
                    "rule": best.rule_id if best else "",
                    "why": (best.rationale or "")[:140] if best else "",
                    "eur": costs.estimate(
                        "omni", max(0.0, best.t_end - best.t_start))
                    if best else 0.0,
                }
            row.append(entry)
        matrix.append({"dimension": dim, "cells": row})

    return _page(request, "timeline.html", run=run, lanes=lanes, ticks=ticks,
                 scenes=scenes, matrix=matrix, embeds=embeds(run, gtheme(request)),
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

    # One card per SCENE, not per observation. A three second scene of a
    # smoking character used to be a scroll of near-identical frames, one
    # per observation. The pipeline already thinks in shots -- "one shot,
    # one edit" is the remediation contract -- so this view does too:
    # the shot's first and last kept frame, and every sentence the analyst
    # wrote inside it, on one card.
    by_shot: dict[str, dict] = {}
    scenes: list[dict] = []
    for row in rows:
        sid = row["obs"].shot_id or row["obs"].id
        scene = by_shot.get(sid)
        if scene is None:
            scene = by_shot[sid] = {
                "shot_id": sid, "rows": [],
                "t_start": row["obs"].t_start, "t_end": row["obs"].t_end,
                "markets": set(), "findings": 0}
            scenes.append(scene)
        scene["rows"].append(row)
        scene["t_start"] = min(scene["t_start"], row["obs"].t_start)
        scene["t_end"] = max(scene["t_end"], row["obs"].t_end)
        scene["findings"] += len(row["findings"])
        scene["markets"].update(row["markets"])
    for scene in scenes:
        scene["working"] = any(f.status == "remediating"
                               for r in scene["rows"] for f in r["findings"])
        framed = [r for r in scene["rows"] if r["frame"]]
        scene["hero"] = framed[0] if framed else None
        # "from screenshot to screenshot": the last kept frame closes the
        # scene, unless it is literally the same still as the first.
        tail = framed[-1] if len(framed) > 1 else None
        if tail is not None and tail["obs"].evidence_frame == scene["hero"]["obs"].evidence_frame:
            tail = None
        scene["tail"] = tail
        scene["markets"] = sorted(scene["markets"])
    scenes.sort(key=lambda sc: sc["t_start"])

    return _page(request, "frame_board.html", run=run, scenes=scenes,
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

@app.get("/ops/busy")
def ops_busy():
    """Is anything running that a deploy would destroy?

    A deploy replaces the single container mid-work: this morning an Omni
    edit finished on the old container seconds after the new one had
    already restored the bucket, so a paid, verified master was silently
    discarded and the next edit rebuilt it from the original. The old
    pre-deploy check only asked whether the newest RUN was done;
    remediations run on any run at any time. scripts/deploy.sh refuses to
    deploy while this answers busy (FORCE_DEPLOY=1 overrides).
    """
    db = store()
    running, remediating, fixing = [], [], []
    for run in db.recent_runs(100):
        if run.status in ("created", "running"):
            running.append(run.id)
        for f in db.findings(run.id):
            if f.status == "remediating":
                remediating.append(f.id)
                if run.id not in fixing:
                    fixing.append(run.id)
    return {"busy": bool(running or remediating),
            "running_runs": running, "remediating_findings": remediating,
            "remediating_runs": fixing}


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
                         or (directory / f"{change.id}_omni.mp4").is_file()
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
        # what this stage is doing, in words a person would use
        group["title"], group["prose"] = narrate.stage(group["agent"])
    made = generated_items(run)
    return _page(request, "mission_feed.html", run=run, events=events,
                 groups=groups, running=run.status not in ("done", "error"),
                 stage_prose=narrate.STAGE_PROSE, made=made,
                 last_id=events[-1]["id"] if events else 0, screen="mission")

# A revoice replaces a spoken line over the same span: the picture is
# untouched and the edit is inaudible as a still. Every other method in
# remediate.METHODS changes what is on screen. /edits splits on this, so
# the audio side can be listened to instead of looked at.
AUDIO_METHODS = ("revoice",)

# One instance, two CPUs, and a cut is an ffmpeg encode. Twenty readers
# hovering twenty cards used to mean twenty encodes racing each other and
# every other request queueing behind them. Two at a time, the rest wait
# their turn -- a cut is about two seconds, so the wait is bounded and the
# page stays answerable.
_CUTTING = threading.BoundedSemaphore(2)


def edited_scenes(limit: int = 120) -> list[dict]:
    """Every scene this instance has edited, newest first, across all runs.

    One row per SCENE, not per change record. Two markets objecting to the
    same two seconds produce two findings and two changes, and as two cards
    they read as two different edits of two different shots -- so a scene
    is one card carrying every market that objected to it and the fix that
    answered them. Which market's fix produced the frame on the right is
    named on the card, because where three markets wanted the same span
    changed, only one of them paid for the render.

    The cutting room answers "what happened to THIS film"; this answers
    "what has this system actually changed", which is the question a reader
    asks after the third run.
    """
    db = store()
    # Same bargain as the archive's cards: this walks every run's changes,
    # findings and two disk checks per change, and ten readers asking at
    # once is that ten times over. Half a minute of staleness on a page
    # about edits that already happened is a fair trade for a page that
    # answers.
    # The store's path is part of the key as well as its fingerprint: two
    # different stores can hold the same number of everything, and the
    # suite proved it by serving one test's scenes to another.
    key = f"{settings.db_path}:{db.stamp()}"
    hit = _SCENES_CACHE.get(key)
    now = time.time()
    if hit and now - hit[0] < _SCENES_TTL:
        return hit[1][:limit]
    scenes: list[dict] = []
    for run in db.recent_runs(200):
        changes = db.changes(run.id)
        if not changes:
            continue
        directory = run_dir(run)
        by_id = {f.id: f for f in db.findings(run.id)}
        grouped: dict[tuple, dict] = {}
        for change in changes:
            finding = by_id.get(change.finding_id)
            kind = "audio" if change.method in AUDIO_METHODS else "video"
            # The span IS the scene's identity: same two seconds, same shot.
            # Except that a revoice and a repaint over the same seconds are
            # two different edits to review -- one you watch, one you
            # listen to -- so the kind is part of the identity too.
            key = ((round(finding.t_start, 1), round(finding.t_end, 1), kind)
                   if finding else ("chg", change.id, kind))
            before = _still_name(directory, change.before_frame)
            after = _still_name(directory, change.after_frame)
            clip = ((directory / "changes" / f"{change.id}_bridge.mp4").is_file()
                    or (directory / "changes" / f"{change.id}_omni.mp4").is_file())
            scene = grouped.setdefault(key, {
                "run": run,
                "asset": Path(run.asset_path).stem or run.asset_path,
                "t_start": finding.t_start if finding else 0.0,
                "t_end": finding.t_end if finding else 0.0,
                "markets": [], "rules": [], "dims": [], "changes": [],
                "before": "", "after": "", "fixed_for": "", "change": None,
                "clip": False, "method": "", "kind": kind,
                "can_before": False, "can_after": False,
            })
            if finding and finding.market and finding.market not in scene["markets"]:
                scene["markets"].append(finding.market)
            if finding and finding.rule_id and finding.rule_id not in scene["rules"]:
                scene["rules"].append(finding.rule_id)
            # What was flagged here, as the taxonomy's own marks: a rule id
            # says which statute, and the mark says what KIND of trouble --
            # a bottle, a hemline, a gesture -- which is the thing a reader
            # recognises across films without reading anything.
            dim = (telemetry._dimension_for(finding.market, finding.rule_id)
                   if finding else "")
            if dim and dim != "none" and dim not in scene["dims"]:
                scene["dims"].append(dim)
            scene["changes"].append(change)
            scene["before"] = scene["before"] or before
            # Whether each side can actually be PLAYED, decided here rather
            # than left to a 404 inside a <video>: an empty grey box says
            # less than the note it replaced. Before needs the original on
            # this disk; after needs the model's own clip or that market's
            # localized master.
            if Path(run.asset_path).is_file() and finding \
                    and finding.t_end > finding.t_start:
                scene["can_before"] = True
            if clip or (finding and (run_dir(run)
                                     / f"localized_{finding.market}.mp4").is_file()):
                scene["can_after"] = True
            # The pair on show is the fix that actually rendered: a change
            # with a clip beats one with only stills, and either beats one
            # that kept nothing at all.
            if (bool(clip), bool(after)) > (scene["clip"], bool(scene["after"])) \
                    or scene["change"] is None:
                scene.update({"after": after or scene["after"], "clip": clip,
                              "change": change, "method": change.method,
                              "fixed_for": finding.market if finding else ""})
        scenes.extend(grouped.values())
    scenes.sort(key=lambda s: ((s["run"].t0 or 0), -s["t_start"]), reverse=True)
    _SCENES_CACHE.clear()          # one store state at a time is enough
    _SCENES_CACHE[key] = (now, scenes)
    return scenes[:limit]


@app.get("/edits", response_class=HTMLResponse)
def my_edits(request: Request, gone: str = "", wrong: str = "",
             again: str = "", why: str = ""):
    """Every scene this instance has edited, before beside after.

    A cross-run cutting room. Each run has one of its own, which is the
    right place to check a film; this is the place to see what the system
    does, which is the question somebody asks once they have watched it do
    it twice.
    """
    rows = edited_scenes()
    # A scene is worth showing if it can be played or if a frame of it was
    # kept. Stills used to be the only test, which dropped every edit whose
    # remediator wrote no frames even though both its spans are cuttable.
    shown = [row for row in rows
             if row["before"] or row["after"]
             or row["can_before"] or row["can_after"]]
    # "carried over from SA", "carried over from DE" and nine more are one
    # method wearing eleven names, and as eleven chips they were the widest
    # thing on the page. Counted together, and counted per scene.
    methods: dict[str, int] = {}
    for row in rows:
        method = row["method"] or "unknown"
        if method.startswith("carried over"):
            method = "carried over"
        methods[method] = methods.get(method, 0) + 1
    return _page(request, "edits.html", screen="edits", rows=shown,
                 gone=gone, wrong=wrong, again=again, why=why,
                 label=remediate.method_label,
                 total=len(rows), unkept=len(rows) - len(shown),
                 edits=sum(len(row["changes"]) for row in rows),
                 films=len({row["asset"] for row in rows}),
                 picture=sum(1 for row in shown if row["kind"] == "video"),
                 sound=sum(1 for row in shown if row["kind"] == "audio"),
                 methods=sorted(methods.items(), key=lambda kv: -kv[1]))


@app.post("/edits/{run_id}/{change_id}/delete")
def delete_edit(request: Request, run_id: str, change_id: str,
                password: str = Form("")):
    """Remove one edit: its record, its stills, its clips and its cuts.

    Behind a word, because this is the one control in the console that
    destroys evidence. Everything else that "deletes" here hides instead;
    this does not, and it cannot be undone from the interface -- the frames
    and the generated seconds are the only copy.

    Why it exists: a fix that came back wrong is still a change record with
    two frames and a clip, and it shows up on My edits beside the ones that
    worked. An operator cleaning up a demo needs to be able to take the bad
    examples out, and doing that by hand in SQLite over a FUSE mount is
    worse than a button with a password on it.

    Removed everywhere at once, because there is one record: the scene
    leaves My edits, the change leaves the cutting room, the generated
    screen and the mission feed, and the run's own numbers stop counting
    it.
    """
    run = _run_or_404(run_id)
    if not re.fullmatch(r"chg_[0-9a-f]{6,32}", change_id):
        raise HTTPException(status_code=404, detail="no such change")
    want = settings.edits_password
    if not want or not secrets.compare_digest(password.strip(), want):
        return RedirectResponse(f"/edits?wrong={quote(change_id)}",
                                status_code=303)
    gone = store().delete_change(run.id, change_id)
    if not gone:
        raise HTTPException(status_code=404, detail="no such change")
    # and the files it wrote, which are named after it
    folder = run_dir(run) / "changes"
    removed = 0
    if folder.is_dir():
        for path in folder.glob(f"{change_id}_*"):
            try:
                path.unlink()
                removed += 1
            except OSError as exc:
                log.warning("could not unlink %s: %s", path.name, exc)
    store().emit(run.id, "operator",
                 f"edit deleted: {change_id} and {removed} file(s)")
    # Straight to the mirror. The store lives on the container's own disk
    # (FUSE has no file locking, so SQLite cannot live on the bucket) and
    # only the pipeline and the remediator used to snapshot it -- so a
    # deletion made between two runs was erased by the next deploy, which
    # is exactly what happened to the first forty-eight of them.
    store().emit(run.id, "operator", persist.snapshot(settings.db_path))
    _SCENES_CACHE.clear()
    _CARD_CACHE.clear()
    return RedirectResponse(f"/edits?gone={quote(change_id)}", status_code=303)


@app.post("/edits/{run_id}/{change_id}/reedit")
def reedit(request: Request, run_id: str, change_id: str,
           background: BackgroundTasks, reason: str = Form("")):
    """Say what is wrong with a fix, and have it done again.

    The verifier can only ask its own question: does the rule still fire?
    A fix can pass that and still be wrong to a person -- the Omni rewrite
    that took a tobacco plug out of a character's hand and left him
    holding something rifle-shaped over his shoulder cleared EU-TOB-01 and
    was not a fix anybody would ship.

    So: the operator's reason IS the instruction. It is written into the
    run's event log first, because the reason a human rejected a machine's
    work is a record worth keeping whatever happens next, and then handed
    to the model as the addition to its prompt. The method is the one that
    made the change, unless that was a patch technique, in which case the
    redo escalates to Omni: asking the thing that produced the object in
    the hand to please not do that again, by the same means, is optimism.

    A finding the verifier had resolved is reopened. That is the honest
    state: a person has just said it is not fixed.
    """
    door = _needs_word(request, "/edits")
    if door is not None:
        return door
    run = _run_or_404(run_id)
    if not re.fullmatch(r"chg_[0-9a-f]{6,32}", change_id):
        raise HTTPException(status_code=404, detail="no such change")
    said = " ".join((reason or "").split())
    if len(said) < 8:
        return RedirectResponse(f"/edits?why={quote(change_id)}", status_code=303)
    db = store()
    change = next((c for c in db.changes(run.id) if c.id == change_id), None)
    if change is None:
        raise HTTPException(status_code=404, detail="no such change")
    finding = next((f for f in db.findings(run.id)
                    if f.id == change.finding_id), None)
    if finding is None:
        raise HTTPException(status_code=404, detail="that change has no finding")
    if finding.remediation_blocked or not finding.remediable:
        raise HTTPException(
            status_code=409,
            detail=finding.blocked_reason or "this finding is not auto-remediable")
    if _role(request) != "judge":
        spent = _visitor_spent(request)
        if spent >= VISITOR_DAILY_EUR:
            raise HTTPException(
                status_code=429,
                detail=(f"You have used your {VISITOR_DAILY_EUR:.2f} EUR of "
                        f"generation for today ({spent:.2f} EUR)."))
    span = max(0.0, finding.t_end - finding.t_start)
    # The method that made it, unless that was a patch: a redo of a patch
    # goes generative, because the words only reach a model that is given
    # the span.
    again = change.method if change.method in ("omni", "bridge") else "omni"
    ok, why = costs.available(again, span, db.spent_today())
    if not ok and again != "bridge":
        again = "bridge"
        ok, why = costs.available(again, span, db.spent_today())
    if not ok:
        raise HTTPException(status_code=409, detail=why)

    db.emit(run.id, "operator",
            f're-edit asked on {change_id} ({finding.rule_id}, '
            f'{finding.market}): "{said}" -> {again}')
    db.emit(run.id, "operator", persist.snapshot(settings.db_path))
    db.update_finding_status(finding.id, "remediating", run.id)
    background.add_task(remediate_and_verify, run.id, finding.id,
                        finding.market, method=again, replacement=said)
    _SCENES_CACHE.clear()
    _CARD_CACHE.clear()
    return RedirectResponse(f"/edits?again={quote(change_id)}", status_code=303)


@app.get("/runs/{run_id}/generated", response_class=HTMLResponse)
def generated_page(request: Request, run_id: str):
    """What the models made for this run, beside the cutting room.

    It was a second tab on the mission feed, which is the event log: "what
    is happening" and "what came out of it" are different questions, and
    the second one is about the film. So it sits next to the two masters.
    """
    run = _run_or_404(run_id)
    return _page(request, "generated.html", run=run,
                 made=generated_items(run), screen="generated")


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

# What a person is allowed to decide about a finding the Guard refused,
# and what each decision means to the clearance.
#
#   waive     a human accepts the risk for this market. It stops holding
#             clearance, because that is what accepting the risk MEANS, and
#             the reason is recorded everywhere the finding is.
#   escalate  it goes to legal review and keeps blocking, because nobody
#             has decided anything yet.
#   manual    the fix will happen in an editing suite rather than here. It
#             keeps blocking until the verifier sees it gone; the markers
#             export beside this button is how it gets to the editor.
_DECISIONS = {
    "waive": ("waived", "waived for this market"),
    "escalate": ("open", "escalated to legal review"),
    "manual": ("open", "approved for a manual edit"),
}


@app.post("/runs/{run_id}/findings/{finding_id}/decision")
def finding_decision(request: Request, run_id: str, finding_id: str,
                     outcome: str = Form(""), reason: str = Form("")):
    """Record what a human decided about a finding this system will not edit.

    The Guard's refusal used to be the end of the story: it named the
    problem, cited the statute, handed it to a person, and offered them
    three disabled buttons. The decision is the most important fact in the
    run and it existed nowhere.

    Gated, like everything that changes an outcome. Written to the
    finding's status, to the run's feed, and to Grafana as an annotation on
    the finding's own marker, so the timeline carries who decided what and
    when.
    """
    door = _needs_word(request, f"/runs/{run_id}")
    if door is not None:
        return door
    run = _run_or_404(run_id)
    choice = (outcome or "").strip()
    if choice not in _DECISIONS:
        raise HTTPException(status_code=400,
                            detail=f"unknown outcome: {choice or '(none)'}")
    db = store()
    finding = next((f for f in db.findings(run.id) if f.id == finding_id), None)
    if finding is None:
        raise HTTPException(status_code=404, detail="no finding with that id")
    if finding.status == "resolved":
        raise HTTPException(status_code=409,
                            detail="this finding is already fixed and verified")
    status, phrase = _DECISIONS[choice]
    note = (reason or "").strip()[:400]
    if choice == "waive" and not note:
        raise HTTPException(
            status_code=400,
            detail="a waiver needs a reason: it is the only record of why "
                   "this market shipped with the finding open")
    who = _role(request) or "visitor"
    db.update_finding_status(finding.id, status, run_id=run.id)
    db.emit(run.id, "guard",
            f"{phrase} by {who}: {finding.rule_id} ({finding.market})"
            + (f" -- {note}" if note else ""))
    try:
        telemetry.annotate_decision(run, finding, choice, note, who)
        # And the market's status again, because a waiver changes it: the
        # finding stops being open, customs_blocking stops carrying its
        # sample, and Grafana resolves its own alert -- the same mechanism
        # a verified fix uses, for a decision a person made instead.
        after = db.findings(run.id, finding.market)
        telemetry.push_status(run, finding.market,
                              adjudicate.clearance(after), after)
    except Exception as exc:  # noqa: BLE001 -- the decision stands regardless
        log.warning("decision telemetry failed for %s: %s", finding.id, exc)
    # A human overruling the machine is the last record that should
    # evaporate on a deploy, and until now it would have: only the pipeline
    # and the remediator ever snapshotted the store to the mirror.
    db.emit(run.id, "guard", persist.snapshot(settings.db_path))
    return RedirectResponse(f"/runs/{run.id}/markets/{finding.market}",
                            status_code=303)


@app.get("/runs/{run_id}/markets/{market}/certificate.pdf")
def market_certificate(run_id: str, market: str):
    """This market's decision as a document, with every finding in it.

    Every other output of this system is a screen. A clearance desk's
    output is a piece of paper: what was cleared, for where, on what date,
    against which statutes, and what was changed to get there. The
    verifier's own sentences are lifted out of the run's event log rather
    than paraphrased, so the certificate quotes the system.

    Open, like every other read here. It names a market this run actually
    covers or it 404s, which is what keeps the path safe.
    """
    run = _run_or_404(run_id)
    if market not in run.markets:
        raise HTTPException(status_code=404,
                            detail=f"run {run_id} does not cover {market}")
    pack = market_packs().get(market)
    if pack is None:
        raise HTTPException(status_code=404,
                            detail=f"no market pack for {market}")
    db = store()
    findings = db.findings(run.id, market)
    changes = [c for c in db.changes(run.id)
               if c.finding_id in {f.id for f in findings}]
    # The verifier writes one sentence per finding it ruled on ("fixed:
    # FR-ALC-01 no longer fires at ...; fnd_x resolved"). Last word wins:
    # a finding can be ruled on more than once across passes.
    lines: dict[str, str] = {}
    for _id, _ts, agent, message in db.events_since(run.id, 0):
        if agent != "verifier":
            continue
        for finding in findings:
            if finding.id in message:
                lines[finding.id] = message
    pdf = certificate.render(
        run, pack, adjudicate.clearance(findings), findings, changes, lines,
        asset_name=Path(run.asset_path).name,
        instance="The Media Customs")
    stamp = time.strftime("%Y%m%d", time.gmtime())
    name = f"clearance-{market}-{Path(run.asset_path).stem}-{stamp}.pdf"
    return Response(pdf, media_type="application/pdf", headers={
        "Content-Disposition": f'inline; filename="{name}"'})


@app.get("/runs/{run_id}/markets/{market}/markers.{fmt}")
def market_markers(run_id: str, market: str, fmt: str):
    """This market's findings as markers, for the suite the fix happens in.

    The console can fix a span itself and often should. For the rest, the
    person who fixes it has the master open in Resolve or Premiere, and
    what they need is the timecodes on their own timeline, in the right
    colour, with the statute in the note. csv or edl; both carry the frame
    rate they were written at, because HH:MM:SS:FF means nothing without
    it.
    """
    if fmt not in ("csv", "edl"):
        raise HTTPException(status_code=404, detail="markers are csv or edl")
    run = _run_or_404(run_id)
    if market not in run.markets:
        raise HTTPException(status_code=404,
                            detail=f"run {run_id} does not cover {market}")
    findings = store().findings(run.id, market)
    asset = Path(run.asset_path)
    fps = media.probe_fps(asset) if asset.is_file() else 25.0
    stem = f"{market}-{asset.stem}"
    if fmt == "csv":
        body = markers.as_csv(findings, fps, asset=asset.name)
        media_type = "text/csv"
    else:
        body = markers.as_edl(findings, fps, title=f"CUSTOMS {market} {asset.stem}")
        media_type = "text/plain"
    return Response(body, media_type=f"{media_type}; charset=utf-8", headers={
        # An editor wants the file, not a tab full of it.
        "Content-Disposition": f'attachment; filename="markers-{stem}.{fmt}"',
        # The rate is in the file, and in the response, because a marker
        # list at the wrong rate drifts a frame a second and looks correct.
        "X-Customs-Fps": f"{fps:g}",
    })


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
    # The market's findings, clustered by SCENE, same as the frame board:
    # a three second shot with four objections is one section with one
    # thumbnail, not four table rows competing for attention. The
    # scenescroll on top jumps straight to a scene's findings.
    obs_by_id = {o.id: o for o in store().observations(run.id)}
    visible = [f for f in findings if not f.remediation_blocked]
    scene_map: dict[str, dict] = {}
    scenes: list[dict] = []
    for f in sorted(visible, key=lambda f: (f.t_start, f.id)):
        obs = obs_by_id.get(f.observation_id)
        sid = (getattr(f, "shot_id", "") or (obs.shot_id if obs else "")
               or f.observation_id)
        sc = scene_map.get(sid)
        if sc is None:
            sc = scene_map[sid] = {"shot_id": sid, "t_start": f.t_start,
                                   "t_end": f.t_end, "findings": [],
                                   "open": 0, "hero": None}
            scenes.append(sc)
        sc["findings"].append(f)
        sc["t_start"] = min(sc["t_start"], f.t_start)
        sc["t_end"] = max(sc["t_end"], f.t_end)
        if f.status == "open":
            sc["open"] += 1
        if sc["hero"] is None and f.observation_id in evidence:
            sc["hero"] = f.observation_id
    # How the scene opens and how it closes, from the SHOT's own kept
    # frames rather than from the findings' -- a market that objected once,
    # halfway through, would otherwise show that one frame as the whole
    # scene. Same pair the frame board shows, and the tail is dropped when
    # it is the same still as the head.
    by_shot: dict[str, list] = {}
    for obs in sorted(obs_by_id.values(), key=lambda o: (o.t_start, o.id)):
        if obs.id in evidence:
            by_shot.setdefault(obs.shot_id or obs.id, []).append(obs.id)
    for sc in scenes:
        sc["findings"].sort(key=lambda f: (-f.severity, f.t_start))
        kept = by_shot.get(sc["shot_id"], [])
        sc["head"] = kept[0] if kept else sc["hero"]
        sc["tail"] = kept[-1] if len(kept) > 1 else None
        # a scene with work in flight opens itself: nobody should have to
        # go looking for the fix they just started
        sc["working"] = any(f.status == "remediating" for f in sc["findings"])

    return _page(request, "market_room.html", run=run, market=market,
                 scenes=scenes,
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
def remediate_now(request: Request, run_id: str, finding_id: str,
                  background: BackgroundTasks,
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
    door = _needs_word(request, f"/runs/{run_id}")
    if door is not None:
        return door
    run = _run_or_404(run_id)
    finding = next((f for f in store().findings(run.id) if f.id == finding_id), None)
    if finding is None or finding.status != "open":
        raise HTTPException(status_code=404, detail="no open finding with that id")
    # A visitor has their own ceiling. The instance budget stops the day
    # running away; this stops one visitor spending everyone else's.
    # Everyone who is not the judge is held to it, so an unrecognised word
    # in the role cookie buys no more than the visitor door does.
    # What the operator typed becomes part of a generation prompt, so it is
    # bounded like any other model input rather than trusted to be short.
    replacement = (replacement or "")[:280]
    if _role(request) != "judge":
        spent = _visitor_spent(request)
        if spent >= VISITOR_DAILY_EUR:
            raise HTTPException(
                status_code=429,
                detail=(f"You have used your {VISITOR_DAILY_EUR:.2f} EUR of "
                        f"generation for today ({spent:.2f} EUR). The findings, "
                        f"the statutes and every run already here stay open to "
                        f"read."))
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
    # What the analyst said before the edit, and what it said after. The
    # verifier re-observes every shot a change touched and stores those
    # observations with a per-verification suffix on the id, so the "after"
    # sentence is a real caption from a real pass rather than a claim: the
    # newest re-observation of the same shot in the same dimension.
    observations = store().observations(run.id)
    said = {o.id: o for o in observations}
    # verify.py stamps a re-observation as "{original id}_v{6 hex}", one
    # token per verification pass, which is what distinguishes a caption
    # written after an edit from the one that found the problem.
    reobserved: dict[tuple[str, str], object] = {}
    for obs in observations:
        if re.fullmatch(r"v[0-9a-f]{6}", obs.id.rsplit("_", 1)[-1]):
            reobserved[(obs.shot_id, obs.dimension)] = obs
    changes = []
    for change in store().changes(run.id):
        finding = by_id.get(change.finding_id)
        before_obs = said.get(finding.observation_id) if finding else None
        after_obs = None
        if before_obs is not None:
            after_obs = reobserved.get((before_obs.shot_id, before_obs.dimension))
        changes.append({
            "change": change,
            "finding": finding,
            "market": finding.market if finding else "",
            "before": _still_name(directory, change.before_frame),
            "after": _still_name(directory, change.after_frame),
            # the box is drawn over the before frame by the browser, from
            # the observation that found the thing in the first place
            "box_for": before_obs.id if before_obs is not None else "",
            "said_before": before_obs.statement if before_obs is not None else "",
            "said_after": after_obs.statement if after_obs is not None else "",
            # only a bridge leaves generated footage behind
            "generated": (directory / "changes" / f"{change.id}_bridge.mp4").is_file()
                         or (directory / "changes" / f"{change.id}_omni.mp4").is_file(),
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
def evidence_frame(run_id: str, observation_id: str, w: int = 0):
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
    if w:
        # A strip of 42px thumbnails has no business pulling megabytes of
        # PNG. Widths are clamped to a short list so the cache cannot be
        # filled by asking for every integer.
        want = min((c for c in (160, 320, 480, 640) if c >= w), default=640)
        thumb = path.with_name(f"{path.stem}_w{want}.jpg")
        try:
            return FileResponse(media.thumbnail(path, want, thumb),
                                media_type="image/jpeg",
                                headers={"Cache-Control": "public, max-age=86400"})
        except Exception as exc:  # noqa: BLE001 -- a thumb is an optimisation
            log.warning("thumbnail failed for %s: %s", observation_id, exc)
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
    # Snapped to a quarter second, and bounded by the longest film this
    # console accepts. Unclamped, every distinct float was its own cache
    # file and its own ffmpeg: ?at=1.0001, 1.0002, ... is an unauthenticated
    # way to fill the container's disk -- which is RAM here -- and pin the
    # CPU of the one instance.
    at = round(min(max(at, 0.0), MAX_DURATION_S) * 4) / 4
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

@app.get("/runs/{run_id}/preview.mp4")
def run_preview(run_id: str):
    """A few seconds of the whole film, for a card to play on hover.

    Built on the first hover and served from disk after that. The card asks
    for it with preload="none", so thirty-five of these cost nothing until
    somebody actually points at one.
    """
    run = _run_or_404(run_id)
    source = Path(run.asset_path)
    if not source.is_file():
        raise HTTPException(status_code=404, detail="the master is gone")
    cached = run_dir(run) / "preview.mp4"
    try:
        made = media.preview_clip(source, cached)
    except Exception as exc:  # noqa: BLE001 -- a card without a preview is fine
        log.warning("preview failed for %s: %s", run.id, exc)
        raise HTTPException(status_code=404, detail="no preview") from exc
    return FileResponse(made, media_type="video/mp4",
                        headers={"Cache-Control": "public, max-age=86400"})


@app.get("/runs/{run_id}/spark.svg")
def run_spark(request: Request, run_id: str):
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
    version = chart_version(run)
    cached = run_dir(run) / "charts" / f"spark_{version}.svg"

    def draw():
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
        return spark.sparkline(points, width=280, height=44)

    return _chart_response(request, cached, version, draw)

@app.get("/runs/{run_id}/markets/{market}/spark.svg")
def market_spark(request: Request, run_id: str, market: str):
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
    version = chart_version(run)
    cached = run_dir(run) / "charts" / f"spark_{safe}_{version}.svg"

    def draw():
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
        return spark.statcard(points, value=value, label=label,
                              colour=state_mod.colour_for_severity(peak),
                              width=260, height=76)

    return _chart_response(request, cached, version, draw)


# ---------------------------------------------------------------- chart cache
#
# The charts were cached on a TIMER: ten minutes for the lanes, five for a
# spark. That expires every chart on a page at the same moment, so the next
# visit to the archive fires thirty-eight Loki and Mimir queries at once,
# holds a worker on each, and Cloud Run -- one instance, by design -- turns
# everything else away with "rate exceeded". The container's disk is also
# RAM, so every restart emptied the cache and the next page paid for all of
# it again.
#
# A timer was the wrong question. These charts are a drawing of the run's
# own rows, so they change when the run changes and never otherwise. The
# cache key is now a fingerprint of exactly that, which means a finished run
# is drawn once and never again, and a LIVE run redraws the moment a finding
# lands instead of when a five minute timer happens to lapse. Nothing is
# stale, nothing is thrown away early.
_CHART_LOCKS: dict[str, threading.Lock] = {}
_CHART_LOCKS_GUARD = threading.Lock()


def chart_version(run) -> str:
    """What this run's charts are a picture of, as a short string.

    Cheap on purpose: three counts and a sum, all from SQLite, because a
    page of cards asks for this once per chart. It has to move whenever the
    picture would: a finding arriving, a severity dropping because a fix
    verified, a market judged late, the run finishing.
    """
    try:
        db = store()
        # Every finding's identity, status and severity, because all three
        # move the picture. Counting them was not enough: a fix verifying
        # flips one status from open to resolved without changing how many
        # there are, and that is precisely the moment a lane should redraw.
        findings = sorted((f.id, f.status, int(f.severity), f.market)
                          for f in db.findings(run.id))
        payload = f"{run.status}|{run.t0}|{len(db.changes(run.id))}|{findings}"
    except Exception:  # noqa: BLE001 -- a fingerprint is an optimisation
        return "x"
    return hashlib.sha1(payload.encode()).hexdigest()[:12]


def _chart_lock(key: str) -> threading.Lock:
    """One lock per chart, so a cold container draws each chart once.

    Without it, thirty-eight simultaneous misses are thirty-eight identical
    Grafana round trips, which is the stampede this whole change is about.
    """
    with _CHART_LOCKS_GUARD:
        return _CHART_LOCKS.setdefault(key, threading.Lock())


def _chart_response(request: Request, path: Path, version: str, draw,
                    media_type: str = "image/svg+xml"):
    """Serve a versioned chart: from disk, from the browser, or drawn once.

    The ETag is the version, so a browser that already has this exact
    picture gets 304 and no work happens at all -- which is what turns a
    reload of the archive from thirty-eight queries into thirty-eight
    empty answers.
    """
    etag = f'W/"{version}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    if not path.is_file():
        with _chart_lock(str(path)):
            if not path.is_file():          # another thread may have drawn it
                body = draw()
                if not body:
                    raise HTTPException(status_code=404,
                                        detail="nothing to draw for this run")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(body) if isinstance(body, str) else path.write_bytes(body)
    data = path.read_bytes()
    return Response(content=data, media_type=media_type, headers={
        "ETag": etag,
        # the picture at this version can never change, so let the browser
        # keep it; a new version is a new URL-and-ETag pair anyway
        "Cache-Control": "public, max-age=86400",
    })


@app.get("/runs/{run_id}/lanes.svg")
def run_lanes(request: Request, run_id: str, full: int = 0):
    """This run's problem lanes, as its own image.

    Built here rather than inline in the archive: each chart is a Loki
    query, and doing twenty of them before the page renders is what took
    /runs past a two minute timeout. As a separate URL the page paints
    at once, the browser fetches the charts lazily, and one slow Grafana
    costs a chart instead of the archive.
    """
    run = _run_or_404(run_id)
    version = chart_version(run)
    kind = "lanes_full" if full else "lanes"
    cached = run_dir(run) / "charts" / f"{kind}_{version}.svg"
    return _chart_response(request, cached, version,
                           lambda: problem_lanes(run, compact=not full))

@app.get("/runs/{run_id}/lanes.png")
def run_lanes_grafana(request: Request, run_id: str, theme: str = ""):
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
    # Cached per theme: the two renders are different pictures, and one
    # overwriting the other is how a light panel ends up on Mission.
    want = theme if theme in ("light", "dark") else gtheme(request)
    cached = run_dir(run) / ("lanes.png" if want == "light" else "lanes-dark.png")
    fresh = cached.is_file() and (time.time() - cached.stat().st_mtime) < 600
    if not fresh:
        try:
            from customs.grafana_ops import GrafanaOps
            asset = Path(run.asset_path).stem or run.asset_path
            duration = asset_duration(run) or MAX_DURATION_S
            with GrafanaOps(settings) as ops:
                png = ops.render_png("customs-lanes", 1, run, duration,
                                     width=1200, height=420, theme=want,
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
def still(run_id: str, filename: str, w: int = 0):
    """A before/after still from runs/{run_id}/changes/, and nothing else.

    `w` serves it at the size it is actually drawn. These are
    full-resolution PNGs, over a megabyte each, and /edits shows a hundred
    and thirty of them as video posters -- which a browser fetches eagerly,
    because a poster has no lazy mode. That page alone was asking a
    single-instance service for a hundred megabytes of images at once,
    which is what made it answer 429 to everybody else.
    """
    run = _run_or_404(run_id)
    path = _within(run_dir(run) / "changes", filename)
    if path is None or path.suffix.lower() != ".png" or not path.is_file():
        raise HTTPException(status_code=404, detail="no such still")
    headers = {"Cache-Control": "public, max-age=86400"}
    if w and 32 <= w <= 1600:
        thumb = path.with_name(f"{path.stem}_w{int(w)}.jpg")
        try:
            small = media.thumbnail(path, int(w), thumb)
            return FileResponse(small, media_type="image/jpeg", headers=headers)
        except Exception as exc:  # noqa: BLE001 -- the full frame still works
            log.warning("thumbnail failed for %s: %s", path.name, exc)
    return FileResponse(path, media_type="image/png", headers=headers)
