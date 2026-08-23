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
* The re-observation is persisted only where it produced news. A fresh
  finding that IS the violation being verified is not stored a second time
  (the original finding already carries it, and its status is the answer);
  a fresh finding of some other rule is a genuinely new violation the edit
  introduced or exposed, and design spec section 3 asks this stage to
  confirm "the finding cleared AND nothing new broke", so those are guarded,
  stored open, logged, annotated and counted in the clearance recompute like
  any other finding. Ids of anything re-observed are suffixed with a
  per-verification token first: obs ids come from shot ids and finding ids
  from obs ids, so without that they would collide with the original run's
  rows on the (run_id, id) primary key.
* push_status only. Never push_timeline: it re-picks the run's t0 and would
  strand every customs_risk sample already written on an orphaned clock
  (telemetry.push_timeline's own docstring). The resolving annotations and
  the status series both map onto the t0 the publisher already fixed.
"""
import uuid
from dataclasses import replace
from pathlib import Path

from customs import pipeline, remediate, telemetry
from customs.media import Shot
from customs.packs import load as load_packs
from customs.schema import ChangeRecord, Finding, Observation

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

    The return value answers exactly one question -- did the edits fix what
    they targeted -- and never hides the other half. A violation the edit
    itself introduced or exposed is stored as a real open finding
    (_persist_new_findings), so it holds clearance on its own, reaches the
    dashboards and can fire its own alert, rather than being folded into a
    boolean about a different finding.

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

    # One token per verification pass. analyst.observe_shot mints
    # obs_{shot_id}_{n} from a per-shot index, so re-observing shot_1 produces
    # the same ids the original run already stored; judge() then derives
    # finding ids from those. Suffixing here is what lets anything this pass
    # finds be persisted at all (see the module docstring).
    token = uuid.uuid4().hex[:6]
    observations = [replace(o, id=f"{o.id}_v{token}") for o in observations]

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
    matched: set[int] = set()
    for change, finding in changed:
        survivors = [
            f for f in fresh if f.rule_id == finding.rule_id and _overlaps(f, finding)
        ]
        matched.update(id(f) for f in survivors)
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

    surfaced = _persist_new_findings(
        store, run, market, pack, fresh, matched, observations)

    current = store.findings(run.id, market)
    status = pipeline.clearance(current)
    _emit(store, run.id, f"{market} clearance recomputed -> {status}")
    try:
        telemetry.push_status(run, market, status, current)
        for change in confirmed:
            telemetry.annotate_resolution(run, change, store)
        if surfaced:
            # one annotation query for the whole batch, same as the publisher:
            # the per-finding loop is what got rate limited in Task 12.
            existing = telemetry.existing_annotation_keys(run)
            for finding in surfaced:
                telemetry.push_log(run, finding)
                telemetry.annotate(run, finding, existing)
    except Exception as exc:  # noqa: BLE001 -- a dead Grafana is not a failed fix
        _emit(store, run.id, f"stage_error: verify: telemetry push failed: {exc!r}")

    return all_gone

def _persist_new_findings(store, run, market: str, pack, fresh: list[Finding],
                          matched: set[int], observations: list[Observation]
                          ) -> list[Finding]:
    """Store every fresh finding that is not one of the violations being verified.

    "Nothing new broke" is a question this stage is the only one positioned to
    answer: nothing else ever looks at localized_{market}.mp4 again. So a
    fresh finding that is not a survivor of a remediated violation is treated
    exactly like a finding from a clearance run -- guarded (pipeline.apply_guard,
    so a protected-basis rule arrives already blocked from auto-remediation),
    stored open, and therefore counted by the clearance recompute and visible
    to the alert rules.

    Two things are deliberately NOT stored:

    * a survivor of a remediated violation (`matched`): the original finding
      already carries that violation and its status is the verdict on it.
    * a fresh finding duplicating an open finding this market already holds
      over the same span. Only the touched shots are re-observed, and a
      touched shot can easily hold a second, unremediated violation that the
      original run already recorded; re-recording it would double it on every
      dashboard, annotation and alert instance.

    The observations backing the stored findings are stored with them (already
    id-suffixed by the caller), so no persisted finding points at an
    observation that is not there.
    """
    open_now = [f for f in store.findings(run.id, market) if f.status == "open"]
    novel = [
        f for f in fresh
        if id(f) not in matched
        and not any(e.rule_id == f.rule_id and _overlaps(e, f) for e in open_now)
    ]
    if not novel:
        return []

    guarded = pipeline.apply_guard(novel, pack)
    by_id = {o.id: o for o in observations}
    backing: list[Observation] = []
    seen: set[str] = set()
    for finding in guarded:
        obs = by_id.get(finding.observation_id)
        if obs is not None and obs.id not in seen:
            seen.add(obs.id)
            backing.append(obs)
    store.add_observations(run.id, backing)
    store.add_findings(guarded)
    _emit(store, run.id,
          f"verification surfaced {len(guarded)} new finding(s) in the edited "
          f"shot(s): {', '.join(sorted(f.rule_id for f in guarded))}")
    return guarded
