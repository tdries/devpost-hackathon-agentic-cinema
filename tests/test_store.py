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
