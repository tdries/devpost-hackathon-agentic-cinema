"""The Verifier: the loop back onto the Analyst that makes this a crew.

Design spec section 3: "Remediation is not trusted until the same instrument
that found the problem looks again." So confirm() does not inspect the edit,
does not ask the model "did that work", and does not take the Remediator's
word for anything. It re-runs the real analyst pass on the localized master,
re-judges the market with the real market pack, and asks one question per
remediated finding: is a finding of the same rule still there, over the same
span? If it is, the fix failed and the finding goes back to open. If it is
not, the finding is resolved and the market's clearance is recomputed and
pushed, which is what makes Grafana resolve the alert on its own.

Three deliberate narrowings, each for a reason:

* Only the shots the changes touched are re-observed. A full re-run would
  cost as much as the original clearance and would tell us nothing about the
  shots nobody edited. This is also why a change record is worth having:
  it names the finding, which names the observation, which names the shot.
* The re-observation and re-judgement are NOT persisted. They describe the
  localized master, not the asset the run is about, and their ids collide
  with the originals by construction (obs ids come from shot ids, finding
  ids from obs ids). The run's findings stay as the record of what was
  found; what changes is their status.
* push_status only. Never push_timeline: it re-picks the run's t0 and would
  strand every customs_risk sample already written on an orphaned clock
  (telemetry.push_timeline's own docstring). The resolving annotations and
  the status series both map onto the t0 the publisher already fixed.
"""
from pathlib import Path

from customs import pipeline, remediate, telemetry
from customs.media import Shot
from customs.packs import load as load_packs
from customs.schema import ChangeRecord, Finding

def _emit(store, run_id: str, message: str) -> None:
    store.emit(run_id, "verifier", message)

def _touched_shots(store, run_id: str, findings: list[Finding]) -> list[Shot]:
    """The shots the remediated findings live in, deduplicated, in order.

    A finding names its observation and an observation names its shot, so the
    shot span comes from the observation when it is still in the store. A
    finding whose observation is missing falls back to its own span, which is
    the same span the observation had (judge copies it), just without the
    shot id.
    """
    observations = {o.id: o for o in store.observations(run_id)}
    shots: list[Shot] = []
    seen = set()
    for finding in findings:
        obs = observations.get(finding.observation_id)
        shot = Shot(
            shot_id=obs.shot_id if obs else f"span_{finding.id}",
            t_start=obs.t_start if obs else finding.t_start,
            t_end=obs.t_end if obs else finding.t_end,
        )
        key = (shot.shot_id, round(shot.t_start, 3), round(shot.t_end, 3))
        if key not in seen:
            seen.add(key)
            shots.append(shot)
    return shots

def _unverified(store, run, targets: list[Finding], reason: str) -> bool:
    """Give up on a verification, putting every finding back to open first.

    A finding left at "remediating" is invisible to adjudicate.clearance(), so
    a verifier that bailed out without doing this would leave the market
    looking cleared with the violation still in the master. Unverified means
    unfixed, and unfixed means open.
    """
    for finding in targets:
        store.update_finding_status(finding.id, "open", run_id=run.id)
    _emit(store, run.id,
          f"stage_error: verify: {reason}; {len(targets)} finding(s) back to open")
    return False

def _overlaps(a, b) -> bool:
    return a.t_start < b.t_end and a.t_end > b.t_start

def confirm(run, market: str, changes: list[ChangeRecord], store, workdir) -> bool:
    """Re-observe, re-judge, and rule on every finding these changes claim to fix.

    Returns True only when every remediated finding's violation is gone from
    the localized master. A finding whose rule still fires over the same span
    goes back to status "open" and the alert stays up, which is the honest
    outcome: design spec section 14, "remediation failure leaves the original
    media untouched and the alert unresolved".

    Telemetry failures are reported as events and never change the verdict:
    whether the edit worked is a question about pixels, not about whether
    Grafana was reachable.
    """
    workdir = Path(workdir)
    master = remediate.localized_master(run, market, store)
    if not master.exists():
        _emit(store, run.id, f"stage_error: verify: no localized master for {market}")
        return False

    findings = store.findings(run.id, market)
    by_id = {f.id: f for f in findings}
    changed = [(c, by_id[c.finding_id]) for c in changes if c.finding_id in by_id]
    if not changed:
        _emit(store, run.id,
              f"stage_error: verify: none of {len(changes)} change(s) name a "
              f"{market} finding of run {run.id}")
        return False

    targets = [finding for _c, finding in changed]
    shots = _touched_shots(store, run.id, targets)
    _emit(store, run.id,
          f"verify -> re-observing {len(shots)} touched shot(s) of {master.name}")

    observations = []
    for shot in shots:
        ok, result = pipeline._call_with_retries(
            lambda shot=shot: pipeline.observe_shot(
                master, shot, workdir,
                on_event=lambda agent, message: store.emit(run.id, agent, message),
            )
        )
        if not ok:
            return _unverified(store, run, targets, f"{shot.shot_id}: {result!r}")
        observations.extend(result)

    pack = load_packs().get(market)
    if pack is None:
        return _unverified(store, run, targets, f"no market pack for {market}")

    ok, fresh = pipeline._call_with_retries(
        lambda: pipeline.judge(
            run.id, observations, pack,
            on_event=lambda agent, message: store.emit(run.id, agent, message),
        )
    )
    if not ok:
        return _unverified(store, run, targets, f"re-adjudication failed: {fresh!r}")

    confirmed = []
    all_gone = True
    for change, finding in changed:
        survivors = [
            f for f in fresh if f.rule_id == finding.rule_id and _overlaps(f, finding)
        ]
        if survivors:
            all_gone = False
            store.update_finding_status(finding.id, "open", run_id=run.id)
            _emit(store, run.id,
                  f"NOT fixed: {finding.rule_id} still fires at "
                  f"{finding.t_start:.2f}-{finding.t_end:.2f}s after {change.method}; "
                  f"{finding.id} back to open")
        else:
            store.update_finding_status(finding.id, "resolved", run_id=run.id)
            confirmed.append(change)
            _emit(store, run.id,
                  f"fixed: {finding.rule_id} no longer fires at "
                  f"{finding.t_start:.2f}-{finding.t_end:.2f}s after {change.method}; "
                  f"{finding.id} resolved")

    current = store.findings(run.id, market)
    status = pipeline.clearance(current)
    _emit(store, run.id, f"{market} clearance recomputed -> {status}")
    try:
        telemetry.push_status(run, market, status, current)
        for change in confirmed:
            telemetry.annotate_resolution(run, change, store)
    except Exception as exc:  # noqa: BLE001 -- a dead Grafana is not a failed fix
        _emit(store, run.id, f"stage_error: verify: telemetry push failed: {exc!r}")

    return all_gone
