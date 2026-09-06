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

    # Everything else still open in the shots this pass is about to
    # re-observe. It costs nothing extra to rule on them -- the shot is
    # being watched again anyway, against the same market pack -- and not
    # ruling on them is a real defect: findings inherit their span from the
    # shot, so a shot routinely carries several, and an edit that removed
    # the bottle also removed the second finding about the same bottle.
    # That one stayed open forever, kept the market blocked, and made the
    # fix look like it had failed.
    # Matched by SHOT, not by span. A finding's own span is the shot's
    # span at judging time, but a shot is resolved through its observation
    # and the two can differ once anything has been re-observed -- so
    # comparing spans quietly matches nothing, which is how the first
    # version of this passed its own test by doing no work.
    observations = {o.id: o for o in store.observations(run.id)}
    touched_shot_ids = {s.shot_id for s in shots}
    targeted = {f.id for f in targets}

    def _shot_of(finding):
        obs = observations.get(finding.observation_id)
        return obs.shot_id if obs else getattr(finding, "shot_id", "") or None

    # "open" OR "remediating": _apply_locked moves a shot's siblings to
    # "remediating" when a grouped edit starts, and the release comment
    # below always promised to put them back -- but this filter only ever
    # admitted "open", so a swept sibling was invisible to the one pass
    # meant to rule on it and sat at "Working" forever. Any "remediating"
    # finding in this market IS this operation's own group: confirm runs
    # under the same market lock the edit held.
    bystanders = [
        f for f in findings
        if f.id not in targeted and f.status in ("open", "remediating")
        and _shot_of(f) in touched_shot_ids
    ]
    if bystanders:
        _emit(store, run.id,
              f"verify -> also ruling on {len(bystanders)} other open finding(s) "
              f"in the same shot(s): {', '.join(f.rule_id for f in bystanders)}")
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

    # The bystanders first, so a finding the edit incidentally cleared is
    # reported as cleared rather than left open by nobody having asked.
    # These do not affect `all_gone`: this pass was not asked to fix them,
    # so failing to is not a failure of the change that ran.
    for finding in bystanders:
        survivors = [
            f for f in fresh if f.rule_id == finding.rule_id and _overlaps(f, finding)
        ]
        matched.update(id(f) for f in survivors)
        if survivors:
            # back to open explicitly: a sibling swept into a shot-wide fix
            # was moved to "remediating" when the edit started, and leaving
            # it there would strand it in a status nothing ever clears.
            store.update_finding_status(finding.id, "open", run_id=run.id)
            _emit(store, run.id,
                  f"still open: {finding.rule_id} continues to fire at "
                  f"{finding.t_start:.2f}-{finding.t_end:.2f}s; {finding.id} untouched")
        else:
            store.update_finding_status(finding.id, "resolved", run_id=run.id)
            _emit(store, run.id,
                  f"incidentally fixed: {finding.rule_id} no longer fires at "
                  f"{finding.t_start:.2f}-{finding.t_end:.2f}s; {finding.id} resolved "
                  f"without having been the target")

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


def _own_edit_overlaps(store, run, market: str, lo: float, hi: float) -> bool:
    """Has THIS market already edited these seconds for itself?

    Its cut is its own answer to its own pack, and splicing another
    market's picture over it would silently undo a fix somebody paid for.
    """
    findings = {f.id: f for f in store.findings(run.id, market)}
    for change in store.changes(run.id):
        finding = findings.get(change.finding_id)
        if finding and finding.t_start < hi and finding.t_end > lo:
            return True
    return False


def spread(run, market: str, changes: list[ChangeRecord], store, workdir) -> list[str]:
    """Carry a confirmed fix into every other market that objected to the
    same seconds. Returns the markets whose cut was updated.

    One asset, every market. The edit is pixels, and pixels do not belong to
    the market that paid for them: when AE's whisky becomes a tea glass, the
    SA finding about the same glass in the same second is about footage that
    no longer exists. It stayed open anyway, and kept SA blocked, because
    confirm() only ever rules on the market it was called for and each
    market carries its own cut -- localized_{market}.mp4.

    So the confirmed seconds are cut out of the market that has them and
    spliced into every other objecting market's cut over the same span, and
    that market's own pack is re-run over the result. Nothing is generated:
    the picture exists and has already been verified once, so this costs the
    day's generation budget nothing at all.

    Two refusals, both deliberate. It never touches a span the other market
    has already edited for itself. And it never resolves a finding on its
    own say-so -- confirm() re-observes and re-judges with that market's
    pack, so a rule that still fires stays open and that market stays
    blocked, which is the same standard the original fix was held to.
    """
    from customs import media  # local: verify is imported by lighter callers

    source = remediate.localized_master(run, market, store)
    if not source.exists():
        return []
    others = [m for m in (run.markets or []) if m != market]
    if not others:
        return []

    workdir = Path(workdir)
    # confirm() is handed a workdir that its caller made; spread() runs
    # after it and cuts a clip of its own, so it cannot assume one exists.
    workdir.mkdir(parents=True, exist_ok=True)
    changes_dir = remediate.run_dir(run, store) / "changes"
    changes_dir.mkdir(parents=True, exist_ok=True)
    carried: list[str] = []

    for change in changes:
        origin = next((f for f in store.findings(run.id, market)
                       if f.id == change.finding_id), None)
        if origin is None:
            continue
        # The span that actually changed is the SHOT's, not the finding's:
        # every method edits the shot it was pointed at, and a finding's own
        # window can be a fraction of it.
        shots = _touched_shots(store, run.id, [origin])
        if not shots:
            continue
        lo = min(s.t_start for s in shots)
        hi = max(s.t_end for s in shots)

        for other in others:
            peers = [f for f in store.findings(run.id, other)
                     if f.status == "open" and f.t_start < hi and f.t_end > lo]
            if not peers:
                continue
            if _own_edit_overlaps(store, run, other, lo, hi):
                _emit(store, run.id,
                      f"{other} objects to the same {lo:.2f}-{hi:.2f}s, but has "
                      f"already edited that span for itself; leaving its cut alone")
                continue

            dest = remediate.localized_master(run, other, store)
            base = dest if dest.exists() else Path(run.asset_path)
            _emit(store, run.id,
                  f"carrying {change.method} from {market} into {other}: "
                  f"{len(peers)} finding(s) on the same {lo:.2f}-{hi:.2f}s "
                  f"({', '.join(p.rule_id for p in peers)}), no generation")
            for peer in peers:
                store.update_finding_status(peer.id, "remediating", run_id=run.id)

            carry_id = f"chg_{uuid.uuid4().hex[:12]}"
            clip = workdir / f"{carry_id}_carry.mp4"
            staged = dest.with_name(f".{carry_id}_{dest.name}")
            try:
                before = remediate._still(base, carry_id, "before", peers[0],
                                          changes_dir)
                media.cut_span(source, lo, hi, clip)
                media.splice_clip(base, clip, lo, hi, staged)
                craft = media.craft_check(base, staged, span=(lo, hi))
                if craft["failures"]:
                    raise RuntimeError("; ".join(craft["failures"]))
                staged.replace(dest)
                after = remediate._still(dest, carry_id, "after", peers[0],
                                         changes_dir)
            except Exception as exc:  # noqa: BLE001 -- one market failing is not all of them
                staged.unlink(missing_ok=True)
                for peer in peers:
                    store.update_finding_status(peer.id, "open", run_id=run.id)
                _emit(store, run.id,
                      f"stage_error: verify: could not carry {change.id} into "
                      f"{other}: {exc!r}; {dest.name} untouched")
                continue
            finally:
                clip.unlink(missing_ok=True)

            record = ChangeRecord(
                id=carry_id, run_id=run.id, finding_id=peers[0].id,
                method=f"carried over from {market}",
                description=(f"the same seconds {market} fixed, spliced into "
                             f"{other}'s cut: {change.description}"),
                before_frame=str(before), after_frame=str(after),
            )
            store.add_change(record)
            carried.append(other)
            # Same standard as the original: this market's own pack decides.
            confirm(run, other, [record], store, workdir)

    return carried
