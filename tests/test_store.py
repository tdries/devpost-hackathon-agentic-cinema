import pytest

from customs.store import Store
from customs.schema import Observation, Finding

def _finding(**overrides):
    fields = dict(
        id="f1", run_id="run1", observation_id="obs1", market="FR",
        rule_id="FR-ALC-01", klass="legal", severity=95,
        t_start=12.4, t_end=14.1, rationale="Loi Evin prohibits alcohol ads on TV",
        citation_ref="Code de la sante publique art. L3323-2",
        citation_url="https://example.org/fr-alc-01",
        sourced=True, remediable=True, remediation_blocked=False,
        blocked_reason="",
    )
    fields.update(overrides)
    return Finding(**fields)

def test_create_run_round_trip(tmp_path):
    s = Store(tmp_path / "t.db")
    run = s.create_run(asset_path="ad.mp4", markets=["FR", "US"])
    assert run.asset_path == "ad.mp4"
    assert run.markets == ["FR", "US"]
    assert run.t0 is None
    assert run.status == "created"

    fetched = s.get_run(run.id)
    assert fetched == run

def test_observations_round_trip_preserves_floats(tmp_path):
    s = Store(tmp_path / "t.db")
    run = s.create_run(asset_path="ad.mp4", markets=["FR"])
    obs = [
        Observation(
            id="obs1", shot_id="shot1", t_start=1.23456789, t_end=4.5,
            dimension="alcohol_tobacco_drugs", statement="wine glass visible",
            evidence_frame="frames/shot1_0001.jpg", confidence=0.874321,
        ),
        Observation(
            id="obs2", shot_id="shot1", t_start=0.0, t_end=0.1,
            dimension="text_legibility", statement="on-screen text in English",
            evidence_frame="frames/shot1_0002.jpg", confidence=1.0,
        ),
    ]
    s.add_observations(run.id, obs)
    fetched = s.observations(run.id)
    assert fetched == obs
    assert [o.t_start for o in fetched] == [1.23456789, 0.0]
    assert [o.confidence for o in fetched] == [0.874321, 1.0]

def test_findings_filtered_by_market(tmp_path):
    s = Store(tmp_path / "t.db")
    run = s.create_run(asset_path="ad.mp4", markets=["FR", "SA"])
    s.add_findings([
        _finding(id="f1", run_id=run.id, market="FR"),
        _finding(id="f2", run_id=run.id, market="SA", rule_id="SA-MOD-02", klass="policy"),
    ])

    fr = s.findings(run.id, market="FR")
    assert [f.id for f in fr] == ["f1"]

    sa = s.findings(run.id, market="SA")
    assert [f.id for f in sa] == ["f2"]

    everything = s.findings(run.id)
    assert {f.id for f in everything} == {"f1", "f2"}

def test_update_finding_status(tmp_path):
    s = Store(tmp_path / "t.db")
    run = s.create_run(asset_path="ad.mp4", markets=["FR"])
    s.add_findings([_finding(id="f1", run_id=run.id)])
    assert s.findings(run.id)[0].status == "open"

    s.update_finding_status("f1", "remediating")
    assert s.findings(run.id)[0].status == "remediating"

    s.update_finding_status("f1", "resolved")
    assert s.findings(run.id)[0].status == "resolved"

def test_events_since(tmp_path):
    s = Store(tmp_path / "t.db")
    run = s.create_run(asset_path="a.mp4", markets=["FR"])
    s.emit(run.id, "analyst", "shot 1 observed")
    s.emit(run.id, "analyst", "shot 2 observed")
    evs = s.events_since(run.id, after_id=0)
    assert [e[3] for e in evs] == ["shot 1 observed", "shot 2 observed"]
    assert s.events_since(run.id, after_id=evs[-1][0]) == []


def test_two_runs_can_hold_identically_named_observations_and_findings(tmp_path):
    """The second run of anything into one Store must not collide.

    Observation ids come from a per-video shot index (obs_shot_0_000 for the
    first observation of any video's first shot) and finding ids are built on
    top of them, so identical ids across runs are the normal case, not an
    edge case. Before the (run_id, id) primary key this raised
    sqlite3.IntegrityError on the second run and took the whole clearance
    down with it.
    """
    s = Store(tmp_path / "t.db")
    run_a = s.create_run(asset_path="a.mp4", markets=["FR"])
    run_b = s.create_run(asset_path="b.mp4", markets=["FR"])

    def canned(run_id):
        obs = Observation(
            id="obs_shot_0_000", shot_id="shot_0", t_start=0.0, t_end=2.0,
            dimension="alcohol_tobacco_drugs", statement="A wine glass is visible.",
            evidence_frame="f.jpg", confidence=0.9,
        )
        fnd = Finding(
            id="fnd_FR_FR-ALC-01_obs_shot_0_000", run_id=run_id,
            observation_id="obs_shot_0_000", market="FR", rule_id="FR-ALC-01",
            klass="legal", severity=95, t_start=0.0, t_end=2.0,
            rationale="Loi Evin", citation_ref="ref", citation_url="https://example.org/x",
            sourced=True, remediable=True, remediation_blocked=False, blocked_reason="",
        )
        return obs, fnd

    for run in (run_a, run_b):
        obs, fnd = canned(run.id)
        s.add_observations(run.id, [obs])
        s.add_findings([fnd])

    assert [o.id for o in s.observations(run_a.id)] == ["obs_shot_0_000"]
    assert [o.id for o in s.observations(run_b.id)] == ["obs_shot_0_000"]
    assert len(s.findings(run_a.id)) == len(s.findings(run_b.id)) == 1
    assert s.findings(run_a.id)[0].run_id == run_a.id
    assert s.findings(run_b.id)[0].run_id == run_b.id

def test_update_finding_status_refuses_an_id_that_exists_in_two_runs(tmp_path):
    # Without run_id a bare UPDATE ... WHERE id = ? would rewrite both runs'
    # rows, stamping one run's data (run_id included) over the other's.
    s = Store(tmp_path / "t.db")
    run_a = s.create_run(asset_path="a.mp4", markets=["FR"])
    run_b = s.create_run(asset_path="b.mp4", markets=["FR"])
    for run in (run_a, run_b):
        s.add_findings([_finding(id="f1", run_id=run.id)])

    with pytest.raises(ValueError, match="exists in 2 runs"):
        s.update_finding_status("f1", "resolved")

    s.update_finding_status("f1", "resolved", run_id=run_a.id)
    assert s.findings(run_a.id)[0].status == "resolved"
    assert s.findings(run_b.id)[0].status == "open", "the other run must be untouched"


# --- task 14: the alert webhook's label lookup ---

def _labelled_finding(store, run, **overrides):
    fields = dict(
        id=f"fnd_{overrides.get('rule_id', 'FR-ALC-01')}", run_id=run.id,
        observation_id="obs_shot_0_000", market="FR", rule_id="FR-ALC-01",
        klass="legal", severity=95, t_start=0.0, t_end=7.0, rationale="r",
        citation_ref="basis", citation_url="https://example.org/x", sourced=True,
        remediable=True, remediation_blocked=False, blocked_reason="", status="open",
    )
    fields.update(overrides)
    finding = Finding(**fields)
    store.add_findings([finding])
    return finding

def test_open_finding_by_labels_matches_on_the_asset_stem(tmp_path):
    store = Store(tmp_path / "s.db")
    run = store.create_run(asset_path="docs/samples/test_ad.mp4", markets=["FR"])
    finding = _labelled_finding(store, run)

    found = store.open_finding_by_labels("test_ad", "FR", "FR-ALC-01")

    assert found is not None
    assert found[0].id == run.id and found[1].id == finding.id

def test_open_finding_by_labels_skips_a_finding_that_is_not_open(tmp_path):
    store = Store(tmp_path / "s.db")
    run = store.create_run(asset_path="docs/samples/test_ad.mp4", markets=["FR"])
    finding = _labelled_finding(store, run)
    store.update_finding_status(finding.id, "resolved", run_id=run.id)

    assert store.open_finding_by_labels("test_ad", "FR", "FR-ALC-01") is None

def test_open_finding_by_labels_returns_none_for_labels_that_match_nothing(tmp_path):
    store = Store(tmp_path / "s.db")
    run = store.create_run(asset_path="docs/samples/test_ad.mp4", markets=["FR"])
    _labelled_finding(store, run)

    assert store.open_finding_by_labels("test_ad", "FR", "FR-NOPE-99") is None
    assert store.open_finding_by_labels("test_ad", "SA", "FR-ALC-01") is None
    assert store.open_finding_by_labels("other_ad", "FR", "FR-ALC-01") is None

def test_open_finding_by_labels_prefers_the_newest_run(tmp_path):
    store = Store(tmp_path / "s.db")
    old_run = store.create_run(asset_path="docs/samples/test_ad.mp4", markets=["FR"])
    _labelled_finding(store, old_run)
    new_run = store.create_run(asset_path="docs/samples/test_ad.mp4", markets=["FR"])
    _labelled_finding(store, new_run)

    found = store.open_finding_by_labels("test_ad", "FR", "FR-ALC-01")

    assert found is not None and found[0].id == new_run.id


def test_store_survives_concurrent_threads(tmp_path):
    """Regression: two threads sharing one connection used to raise
    sqlite3.InterfaceError ("bad parameter or other API misuse") and, worse,
    sometimes return None for a run that exists. Both were observed from two
    concurrent remediations of one run; Store now serializes on its own lock.
    """
    import threading

    store = Store(tmp_path / "s.db")
    run = store.create_run(asset_path="docs/samples/test_ad.mp4", markets=["FR"])
    errors = []

    def hammer(tag):
        try:
            for i in range(40):
                store.emit(run.id, "test", f"{tag}-{i}")
                assert store.get_run(run.id) is not None
                store.findings(run.id, "FR")
                store.observations(run.id)
        except Exception as exc:  # noqa: BLE001 -- the assertion is "none of these"
            errors.append(exc)

    threads = [threading.Thread(target=hammer, args=(tag,)) for tag in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    assert len(store.events_since(run.id, 0)) == 80


def test_recent_runs_lists_the_newest_first_and_includes_a_run_never_started(tmp_path):
    """The console's front door reads this. A run created a second ago and
    never started has no t0, and it is exactly the one someone is looking
    for, so ordering cannot depend on t0 being set."""
    store = Store(tmp_path / "t.db")
    first = store.create_run(asset_path="a.mp4", markets=["FR"])
    store.set_run_t0(first.id, 1000.0)
    second = store.create_run(asset_path="b.mp4", markets=["SA", "US"])

    listed = store.recent_runs()

    assert [r.id for r in listed] == [second.id, first.id]
    assert listed[0].t0 is None
    assert listed[0].markets == ["SA", "US"]
    assert len(store.recent_runs(limit=1)) == 1
