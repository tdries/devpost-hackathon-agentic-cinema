"""The Customs HTTP service. Right now: the Grafana alert webhook, and
nothing else.

This is the seam where Grafana stops being a report and becomes the thing
that wakes the agent (design spec section 9b: "Grafana is upstream of the
work, not a report produced afterwards"). An alert rule fires, its contact
point POSTs here, and this service remediates the finding the alert is
about and verifies the fix, which drops the metric and lets Grafana resolve
its own alert.

Two rules govern this file:

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

Task 15 builds the Launch Control console around this file. The route below
is the part that must survive that: keep POST /webhook/alert.
"""
import logging
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Request

from customs import remediate, verify
from customs.config import settings
from customs.store import Store

log = logging.getLogger("customs.app")

app = FastAPI(title="Customs", description="Ad clearance crew: alert webhook")

# Scratch space for the frames and audio remediation extracts. Same default
# the CLI uses (scripts/run_pipeline.py --workdir).
WORKDIR = Path("runs/work")

_store_singleton: Store | None = None

def store() -> Store:
    """The run store, opened once per process (telemetry.py's pattern)."""
    global _store_singleton
    if _store_singleton is None:
        _store_singleton = Store(settings.db_path)
    return _store_singleton

def remediate_and_verify(run_id: str, finding_id: str, market: str,
                         workdir=None) -> bool:
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
            method = remediate.plan(finding, observation)
            db.emit(run_id, "remediator",
                    f"alert on {finding.rule_id} ({market}) -> planned {method}")
            change = remediate.apply(run, finding, method, workdir, db)
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
async def healthz() -> dict:
    """Liveness for Cloud Run. Deliberately touches nothing."""
    return {"status": "ok"}
