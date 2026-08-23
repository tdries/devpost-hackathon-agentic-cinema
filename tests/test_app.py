"""Alert webhook tests, offline.

The store is pointed at a tmp database and the remediation job is replaced by
a recorder, so these tests are about exactly one thing: which alerts turn
into work, and which are dropped. TestClient runs a BackgroundTask after the
response has been returned, so a recorded call proves the task was really
enqueued rather than just planned.
"""
import pytest
from fastapi.testclient import TestClient

from customs import app as app_module
from customs.schema import Finding
from customs.store import Store

ASSET = "docs/samples/test_ad.mp4"

def _finding(run_id, **overrides):
    fields = dict(
        id="fnd_FR_FR-ALC-01_obs_shot_0_000", run_id=run_id,
        observation_id="obs_shot_0_000", market="FR", rule_id="FR-ALC-01",
        klass="legal", severity=95, t_start=0.0, t_end=7.0,
        rationale="wine glasses", citation_ref="Loi Evin",
        citation_url="https://example.org/evin", sourced=True, remediable=True,
        remediation_blocked=False, blocked_reason="", status="open",
    )
    fields.update(overrides)
    return Finding(**fields)

@pytest.fixture
def client(tmp_path, monkeypatch):
    """A test client over a tmp store holding one open FR-ALC-01 finding on
    test_ad, plus the list every enqueued remediation lands in."""
    store = Store(tmp_path / "customs.db")
    run = store.create_run(asset_path=ASSET, markets=["FR", "SA"])
    store.add_findings([_finding(run.id)])
    monkeypatch.setattr(app_module, "_store_singleton", store)

    jobs = []
    monkeypatch.setattr(
        app_module, "remediate_and_verify",
        lambda run_id, finding_id, market, workdir=None: jobs.append(
            (run_id, finding_id, market)),
    )
    with TestClient(app_module.app) as test_client:
        yield test_client, store, run, jobs

def _alert(**label_overrides):
    labels = {"asset": "test_ad", "market": "FR", "rule_id": "FR-ALC-01"}
    labels.update(label_overrides)
    return {
        "receiver": "customs-webhook",
        "status": "firing",
        "alerts": [{
            "status": "firing",
            "labels": dict(labels, alertname="customs_blocking_finding",
                           grafana_folder="Customs"),
            "annotations": {"summary": "Blocking finding"},
            "valueString": "[ var='B' labels={} value=95 ]",
        }],
    }

def test_valid_alert_answers_200_and_enqueues_remediation(client):
    test_client, _store, run, jobs = client

    response = test_client.post("/webhook/alert", json=_alert())

    assert response.status_code == 200
    assert response.json()["accepted"] == 1
    assert jobs == [(run.id, "fnd_FR_FR-ALC-01_obs_shot_0_000", "FR")]

def test_forged_rule_id_answers_200_and_enqueues_nothing(client):
    test_client, _store, _run, jobs = client

    response = test_client.post("/webhook/alert", json=_alert(rule_id="FR-NOPE-99"))

    assert response.status_code == 200
    assert response.json() == {"accepted": 0, "ignored": 1}
    assert jobs == []

def test_forged_asset_and_market_enqueue_nothing(client):
    test_client, _store, _run, jobs = client

    assert test_client.post("/webhook/alert", json=_alert(asset="someone_elses_ad")).status_code == 200
    assert test_client.post("/webhook/alert", json=_alert(market="ZZ")).status_code == 200
    assert jobs == []

def test_malformed_json_does_not_500(client):
    test_client, _store, _run, jobs = client

    response = test_client.post(
        "/webhook/alert", content=b"{not json at all",
        headers={"Content-Type": "application/json"})

    assert response.status_code == 200
    assert response.json()["accepted"] == 0
    assert jobs == []

def test_payload_without_alerts_array_does_not_500(client):
    test_client, _store, _run, jobs = client

    for body in ({}, {"alerts": "nope"}, {"alerts": [None, 7]}, []):
        response = test_client.post("/webhook/alert", json=body)
        assert response.status_code == 200, body
    assert jobs == []

def test_alert_without_labels_is_ignored(client):
    test_client, _store, _run, jobs = client
    payload = _alert()
    payload["alerts"][0].pop("labels")

    assert test_client.post("/webhook/alert", json=payload).status_code == 200
    assert jobs == []

def test_resolved_alert_starts_no_work(client):
    test_client, _store, _run, jobs = client
    payload = _alert()
    payload["alerts"][0]["status"] = "resolved"

    assert test_client.post("/webhook/alert", json=payload).status_code == 200
    assert jobs == []

def test_body_fields_beyond_the_labels_are_never_trusted(client):
    # the body claims a different finding, of a different market, that does
    # not exist. Only the labels decide, so the real FR-ALC-01 finding is what
    # gets enqueued.
    test_client, _store, run, jobs = client
    payload = _alert()
    payload["alerts"][0]["finding_id"] = "fnd_SA_SA-LGBT-01_obs_shot_6_000"
    payload["alerts"][0]["market"] = "SA"
    payload["alerts"][0]["annotations"]["finding_id"] = "fnd_SA_SA-LGBT-01_obs_shot_6_000"

    test_client.post("/webhook/alert", json=payload)

    assert jobs == [(run.id, "fnd_FR_FR-ALC-01_obs_shot_0_000", "FR")]

def test_an_already_resolved_finding_is_not_remediated_again(client):
    test_client, store, run, jobs = client
    store.update_finding_status("fnd_FR_FR-ALC-01_obs_shot_0_000", "resolved",
                                run_id=run.id)

    assert test_client.post("/webhook/alert", json=_alert()).status_code == 200
    assert jobs == []

def test_the_webhook_records_the_alert_on_the_run(client):
    test_client, store, run, _jobs = client

    test_client.post("/webhook/alert", json=_alert())

    messages = [m for (_i, _t, _a, m) in store.events_since(run.id, 0)]
    assert any("alert received: FR-ALC-01" in m for m in messages), messages

def test_remediate_and_verify_reports_false_for_an_unknown_run(tmp_path, monkeypatch):
    store = Store(tmp_path / "customs.db")
    monkeypatch.setattr(app_module, "_store_singleton", store)
    assert app_module.remediate_and_verify("run_nope", "fnd_nope", "FR") is False

def test_remediate_and_verify_records_a_stage_error_when_the_edit_raises(
        tmp_path, monkeypatch):
    store = Store(tmp_path / "customs.db")
    run = store.create_run(asset_path=ASSET, markets=["FR"])
    store.add_findings([_finding(run.id)])
    monkeypatch.setattr(app_module, "_store_singleton", store)
    monkeypatch.setattr(app_module.remediate, "apply",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("ffmpeg died")))

    result = app_module.remediate_and_verify(
        run.id, "fnd_FR_FR-ALC-01_obs_shot_0_000", "FR", workdir=tmp_path / "work")

    assert result is False
    messages = [m for (_i, _t, _a, m) in store.events_since(run.id, 0)]
    assert any("stage_error: remediate" in m for m in messages), messages


# --- one writer per localized master (see remediate.market_lock) ---

def test_market_lock_is_per_run_and_per_market():
    lock = app_module.remediate.market_lock("run_a", "FR")
    assert app_module.remediate.market_lock("run_a", "FR") is lock
    assert app_module.remediate.market_lock("run_a", "SA") is not lock, "markets write different files"
    assert app_module.remediate.market_lock("run_b", "FR") is not lock, "runs write different files"

def test_two_alerts_for_one_market_serialize_end_to_end(tmp_path, monkeypatch):
    """Two webhook jobs for the same market must not interleave.

    Starlette runs a sync BackgroundTask in a threadpool, so this is what two
    near-simultaneous alerts really look like. Without the lock the second
    thread enters apply() while the first is still between apply and verify,
    reads the master the first one read, and reverts a verified edit.
    """
    import threading

    store = Store(tmp_path / "customs.db")
    run = store.create_run(asset_path=ASSET, markets=["FR"])
    store.add_findings([_finding(run.id), _finding(run.id, id="fnd_two",
                                                   rule_id="FR-LANG-01", severity=60)])
    monkeypatch.setattr(app_module, "_store_singleton", store)

    trace = []
    trace_guard = threading.Lock()

    def record(event):
        with trace_guard:
            trace.append(event)

    class _Change:
        finding_id = "x"

    def slow_apply(run_record, finding, method, workdir, store_arg, **kwargs):
        record(f"apply:{finding.id}")
        threading.Event().wait(0.05)  # long enough that an unlocked second thread interleaves
        return _Change()

    def slow_verify(run_record, market, changes, store_arg, workdir):
        record(f"verify:{market}")
        threading.Event().wait(0.05)
        return True

    monkeypatch.setattr(app_module.remediate, "apply", slow_apply)
    monkeypatch.setattr(app_module.remediate, "plan", lambda finding, observation=None: "reframe")
    monkeypatch.setattr(app_module.verify, "confirm", slow_verify)

    threads = [
        threading.Thread(target=app_module.remediate_and_verify,
                         args=(run.id, finding_id, "FR"),
                         kwargs={"workdir": tmp_path / "work"})
        for finding_id in ("fnd_FR_FR-ALC-01_obs_shot_0_000", "fnd_two")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert len(trace) == 4, trace
    # each job's apply is immediately followed by its own verify: no interleave
    assert trace[1] == "verify:FR" and trace[3] == "verify:FR", trace
    assert trace[0].startswith("apply:") and trace[2].startswith("apply:"), trace
    assert trace[0] != trace[2], "both jobs ran"
