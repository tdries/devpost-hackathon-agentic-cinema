"""The golden test: the spec's quality bar, run live against the real asset.

Design spec section 15: "Model output quality is checked by a small golden
set ... tolerant to wording but not to a market's rules being missed
entirely." docs/samples/landmines.yaml is that golden set -- it records what
was deliberately planted in docs/samples/test_ad.mp4, shot by shot, and which
rule of today's packs each landmine should trip.

Both tests here are live and are excluded from the default run (pyproject's
addopts is -m 'not live'). They also deliberately use the project's real run
store (settings.db_path) rather than a tmp one, for two reasons: the run they
produce is the run the console and the dashboards show, and the remediation
rehearsal below reuses it instead of paying for a second clearance.

    python -m pytest tests/test_golden.py -m live -k clearance -s
    python -m pytest tests/test_golden.py -m live -k relettering -s
"""
import shutil
from pathlib import Path

import pytest
import yaml

from customs import pipeline, remediate, verify
from customs.config import settings
from customs.store import Store

ASSET = "docs/samples/test_ad.mp4"
LANDMINES = Path("docs/samples/landmines.yaml")
MARKETS = ["FR", "SA"]
WORKDIR = Path("runs/work")
SCREENSHOTS = Path("docs/screenshots")

# The demo's scripted edit: shot 2's burned-in English note, re-lettered into
# French so FR-LANG-01 (Loi Toubon) stops firing.
RELETTER_RULE = "FR-LANG-01"
RELETTER_TEXT = "Le bonheur est a une gorgee"

def _landmines() -> list[dict]:
    return yaml.safe_load(LANDMINES.read_text())

def _expected(markets) -> list[tuple[int, float, str]]:
    """(shot, t_approx, rule_id) for every landmine expectation belonging to
    one of `markets`. Rules of other markets' packs are skipped, not failed:
    US-FLASH-01 and US-CMP-01 are real expectations, they are just not
    answerable by a run that did not load the US pack."""
    return [
        (entry["shot"], entry["t_approx"], rule_id)
        for entry in _landmines()
        for rule_id in entry.get("expects", [])
        if rule_id.split("-")[0] in markets
    ]

def _newest_run_with(store: Store, rule_id: str, market: str):
    """The newest run whose `market` findings include an open `rule_id`."""
    match = store.open_finding_by_labels(Path(ASSET).stem, market, rule_id)
    if match is None:
        pytest.skip(
            f"no open {rule_id} finding for {market} in {settings.db_path}; "
            "run the clearance golden test first"
        )
    return match

@pytest.mark.live
def test_golden_clearance_run_finds_every_planted_landmine():
    store = Store(settings.db_path)
    WORKDIR.mkdir(parents=True, exist_ok=True)

    run = pipeline.run(ASSET, MARKETS, store, WORKDIR)
    print(f"\ngolden run: {run.id}")

    assert run.status == "done"
    assert pipeline.errored_markets(store, run.id) == set(), "every market must be evaluated"

    findings = store.findings(run.id)
    for market in MARKETS:
        print(f"{market}: {[f.rule_id for f in findings if f.market == market]}")

    # 1. every landmine whose expectation belongs to this run's markets fired,
    #    on the shot it was planted in.
    misses = []
    for shot, t_approx, rule_id in _expected(MARKETS):
        hits = [
            f for f in findings
            if f.rule_id == rule_id and f.t_start <= t_approx <= f.t_end
        ]
        if not hits:
            misses.append(f"shot {shot}: {rule_id} at {t_approx}s")
    assert not misses, f"planted landmines that did not fire: {misses}"

    # 2. the guard case: SA-LGBT-01 is a protected characteristic, so it must
    #    be found, must be blocked from auto-remediation, and must still be
    #    open (blocking remediation is not the same as closing the finding).
    lgbt = [f for f in findings if f.rule_id == "SA-LGBT-01"]
    assert lgbt, "SA-LGBT-01 (shot 7, protected basis) must be found"
    assert all(f.remediation_blocked for f in lgbt), lgbt
    assert all(f.status == "open" for f in lgbt), lgbt
    assert all(f.blocked_reason for f in lgbt), "a blocked finding must say why"

    # 3. the US-FLASH landmine, at the observation level: the flash detector
    #    measures it deterministically during ingest even though no US pack is
    #    loaded in this run, so there is an adjudicable photosensitivity
    #    observation waiting for the market that has a rule for it.
    flash_shot = next(e for e in _landmines() if e["dimension"] == "photosensitivity_sensory")
    flashes = [
        o for o in store.observations(run.id)
        if o.dimension == "photosensitivity_sensory"
        and o.t_start <= flash_shot["t_approx"] <= o.t_end
    ]
    assert flashes, "the planted strobe must produce a photosensitivity observation"
    print(f"flash observation: {flashes[0].statement}")
    assert "flashes per second" in flashes[0].statement

@pytest.mark.live
def test_live_relettering_loop_closes_the_finding():
    """The demo's closing beat, end to end: plan, edit, verify, resolve.

    Reuses the newest run holding an open FR-LANG-01 finding (the clearance
    test above produces it), re-letters shot 2's English note into French,
    and asks the verifier to confirm it against the localized master. The
    before/after stills are copied into docs/screenshots/ because they are
    the evidence a human checks that the edit is real.
    """
    store = Store(settings.db_path)
    run, finding = _newest_run_with(store, RELETTER_RULE, "FR")
    observation = next(
        (o for o in store.observations(run.id) if o.id == finding.observation_id), None
    )
    print(f"\nrehearsal run: {run.id}, finding: {finding.id}")

    method = remediate.plan(finding, observation)
    assert method == "relettering", f"FR-LANG-01 is text_legibility, got {method}"

    change = remediate.apply(run, finding, method, WORKDIR, store,
                             replacement=RELETTER_TEXT)
    master = remediate.localized_master(run, "FR", store)
    assert master.exists()
    print(f"localized master: {master}")

    SCREENSHOTS.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(change.before_frame, SCREENSHOTS / "02-remediation-before.png")
    shutil.copyfile(change.after_frame, SCREENSHOTS / "03-remediation-after.png")

    confirmed = verify.confirm(run, "FR", [change], store, WORKDIR)
    resolved = next(f for f in store.findings(run.id, "FR") if f.id == finding.id)
    print(f"verifier: {confirmed}, finding status: {resolved.status}, "
          f"FR clearance: {pipeline.clearance(store.findings(run.id, 'FR'))}")

    assert confirmed is True, "the verifier must confirm the re-lettered note"
    assert resolved.status == "resolved"
    # FR may well stay blocked: the alcohol findings on shots 1, 5 and 7 are
    # real and untouched. What must change is this finding, and the clearance
    # recomputation that no longer counts it.
    assert all(
        f.status == "resolved"
        for f in store.findings(run.id, "FR")
        if f.rule_id == RELETTER_RULE and f.id == finding.id
    )
    assert store.changes(run.id), "the change record is the audit trail"
