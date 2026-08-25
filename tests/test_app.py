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
from customs.schema import Finding, Observation
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
        lambda run_id, finding_id, market, workdir=None, **kw: jobs.append(
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


# --- Launch Control console (Task 15) -------------------------------------
#
# Every test below is offline and hermetic: the crew is replaced by a fake
# that writes the same rows a real run would (events, findings, a stage error
# for one market), so the console is exercised against real store shapes
# without a four minute Vertex run.

import asyncio
import json
import os
import re
import threading
import time
from pathlib import Path

from customs.schema import ChangeRecord

MARKETS = ["FR", "SA", "US"]


def _console_findings(run_id):
    """One blocked market, one guard-blocked finding, one clean market."""
    return [
        _finding(run_id),  # FR legal 95 sourced -> blocked
        _finding(run_id, id="fnd_FR_FR-LANG-01_obs_shot_1_000",
                 rule_id="FR-LANG-01", klass="policy", severity=55,
                 rationale="English tagline with no French translation",
                 citation_ref="Loi Toubon art. 2", t_start=7.0, t_end=12.0),
        _finding(run_id, id="fnd_SA_SA-LGBT-01_obs_shot_6_000", market="SA",
                 rule_id="SA-LGBT-01", klass="offence", severity=90,
                 rationale="two men holding hands",
                 citation_ref="GCAM content standards",
                 remediable=False, remediation_blocked=True,
                 blocked_reason="protected basis: a human decides this one"),
    ]


@pytest.fixture
def console(tmp_path, monkeypatch):
    """A console client whose crew is a fast fake.

    Returns (client, store, launched, jobs): `launched` records every call the
    POST /runs handler made into the crew, `jobs` every remediation enqueued.
    """
    store = Store(tmp_path / "customs.db")
    monkeypatch.setattr(app_module, "_store_singleton", store)
    monkeypatch.setattr(app_module, "probe_duration", lambda path: 42.0)

    launched = []

    def fake_launch(run_id, asset_path, markets, workdir=None):
        launched.append((run_id, str(asset_path), list(markets)))
        store.set_run_t0(run_id, time.time())
        store.set_run_status(run_id, "running")
        store.emit(run_id, "pipeline", f"run {run_id} started")
        store.add_findings(_console_findings(run_id))
        store.emit(run_id, "adjudicator", "FR clearance -> blocked (2 finding(s))")
        store.emit(run_id, "adjudicator", "stage_error: market=SA: simulated 5xx")
        store.emit(run_id, "adjudicator", "US clearance -> cleared (0 finding(s))")
        store.set_run_status(run_id, "done")

    monkeypatch.setattr(app_module, "launch_clearance", fake_launch)

    jobs = []
    monkeypatch.setattr(
        app_module, "remediate_and_verify",
        lambda run_id, finding_id, market, workdir=None, **kw: jobs.append(
            (run_id, finding_id, market)),
    )
    with TestClient(app_module.app) as test_client:
        yield test_client, store, launched, jobs


def _judged_run(store):
    """A finished run holding the fixture's findings and stage error."""
    run = store.create_run(asset_path=ASSET, markets=list(MARKETS))
    store.set_run_t0(run.id, time.time())
    store.add_findings(_console_findings(run.id))
    store.emit(run.id, "adjudicator", "FR clearance -> blocked (2 finding(s))")
    store.emit(run.id, "adjudicator", "stage_error: market=SA: simulated 5xx")
    # the Publisher is the last stage of a real run, and the board's Grafana
    # panels are gated on it having run
    store.emit(run.id, "publisher", "push_run_telemetry -> {'risk_samples': '3 market(s)'}")
    store.set_run_status(run.id, "done")
    return store.get_run(run.id)


def _upload(client, payload=b"\x00\x01fake mp4 bytes", markets=MARKETS, name="ad.mp4"):
    return client.post(
        "/runs",
        files={"asset": (name, payload, "video/mp4")},
        data={"markets": list(markets)},
        follow_redirects=False,
    )


# -- the front door --

def test_home_offers_every_market_pack_and_says_when_there_are_no_runs(console):
    client, _store, _launched, _jobs = console

    body = client.get("/").text

    assert client.get("/").status_code == 200
    for market in MARKETS:
        assert f'value="{market}"' in body, market
    assert "no runs yet" in body.lower()


def test_upload_creates_a_run_starts_the_crew_and_redirects_to_the_board(console):
    client, store, launched, _jobs = console

    response = _upload(client)

    assert response.status_code == 303
    run_id = response.headers["location"].rsplit("/", 1)[-1]
    assert store.get_run(run_id) is not None
    for _ in range(100):  # the crew runs on its own thread
        if launched:
            break
        time.sleep(0.05)
    assert launched and launched[0][0] == run_id
    assert launched[0][2] == MARKETS


def test_two_runs_started_together_get_distinct_workdirs(tmp_path, monkeypatch):
    """WORKDIR is one process-global scratch root, but the ingest and analyst
    stages name their scratch frames and audio by shot_id, and every video's
    shots are numbered from 0 (media.detect_shots), so shot_0 exists for
    every asset. Two runs sharing WORKDIR directly would overwrite each
    other's frames mid-run and the Analyst could end up judging the wrong
    video, so _clearance_job must hand each run its own runs/work/{run_id}
    all the way down into crew.run_clearance.

    This drives the real launch_clearance rather than the console fixture's
    fake (which never calls crew.run_clearance at all): only
    crew.run_clearance itself is replaced here, with a spy that records the
    workdir it was called with.
    """
    from customs import crew

    store = Store(tmp_path / "customs.db")
    monkeypatch.setattr(app_module, "_store_singleton", store)
    monkeypatch.setattr(app_module, "probe_duration", lambda path: 42.0)

    calls = []

    def fake_run_clearance(asset_path, markets, store_arg, workdir, *,
                           publish=True, model=None, run_id=None):
        calls.append((run_id, Path(workdir)))
        store_arg.set_run_status(run_id, "done")

    monkeypatch.setattr(crew, "run_clearance", fake_run_clearance)

    with TestClient(app_module.app) as client:
        first = _upload(client)
        second = _upload(client)

    assert first.status_code == 303 and second.status_code == 303
    run_id_1 = first.headers["location"].rsplit("/", 1)[-1]
    run_id_2 = second.headers["location"].rsplit("/", 1)[-1]
    assert run_id_1 != run_id_2

    for _ in range(100):  # each crew runs on its own thread
        if len(calls) >= 2:
            break
        time.sleep(0.05)
    seen = dict(calls)
    assert set(seen) == {run_id_1, run_id_2}, calls

    assert seen[run_id_1] == app_module.WORKDIR / run_id_1
    assert seen[run_id_2] == app_module.WORKDIR / run_id_2
    assert seen[run_id_1] != seen[run_id_2]


def test_an_upload_longer_than_two_minutes_is_rejected_with_a_plain_message(
        console, monkeypatch):
    client, _store, launched, _jobs = console
    monkeypatch.setattr(app_module, "probe_duration", lambda path: 181.0)

    response = _upload(client)

    assert response.status_code == 400
    assert "120" in response.text and "second" in response.text.lower()
    assert launched == []


def test_an_oversize_upload_is_rejected_with_a_plain_message(console, monkeypatch):
    client, _store, launched, _jobs = console
    monkeypatch.setattr(app_module, "MAX_UPLOAD_BYTES", 1024)

    response = _upload(client, payload=b"x" * 4096)

    assert response.status_code == 400
    assert "too large" in response.text.lower()
    assert launched == []


def test_a_rejected_upload_leaves_nothing_behind(console, monkeypatch):
    client, store, _launched, _jobs = console
    monkeypatch.setattr(app_module, "probe_duration", lambda path: 900.0)

    _upload(client)

    assert store.recent_runs() == []
    assert list(app_module.uploads_dir().glob("*/*")) == []


def test_an_upload_naming_no_market_is_rejected(console):
    client, _store, launched, _jobs = console

    response = _upload(client, markets=[])

    assert response.status_code == 400
    assert "market" in response.text.lower()
    assert launched == []


def test_an_upload_naming_an_unknown_market_is_rejected(console):
    client, _store, launched, _jobs = console

    response = _upload(client, markets=["FR", "ZZ"])

    assert response.status_code == 400
    assert launched == []


# -- the board --

def test_every_console_screen_404s_for_an_unknown_run(console):
    client, _store, _launched, _jobs = console

    for path in ("/runs/run_nope", "/runs/run_nope/status", "/runs/run_nope/mission",
                 "/runs/run_nope/feed", "/runs/run_nope/markets/FR",
                 "/runs/run_nope/cutting", "/runs/run_nope/media/original"):
        assert client.get(path).status_code == 404, path


def test_status_reports_a_clearance_a_count_and_the_errored_market(console):
    client, store, _launched, _jobs = console
    run = _judged_run(store)

    body = client.get(f"/runs/{run.id}/status").json()

    assert body["done"] is True
    assert set(body["markets"]) == set(MARKETS)
    assert body["markets"]["FR"] == {
        "clearance": "blocked", "display": "blocked", "findings": 2, "open": 2,
        "working": 0, "resolved": 0, "blocked": 0, "errored": False}
    assert body["markets"]["SA"]["errored"] is True
    assert body["markets"]["SA"]["blocked"] == 1
    assert body["markets"]["US"]["clearance"] == "cleared"
    assert body["overall"]["cleared"] == 1
    assert body["overall"]["total"] == 3
    assert body["overall"]["failing"] == ["FR", "SA"]


def test_a_market_with_no_verdict_yet_reads_as_pending(console):
    client, store, _launched, _jobs = console
    run = store.create_run(asset_path=ASSET, markets=list(MARKETS))
    store.set_run_status(run.id, "running")

    body = client.get(f"/runs/{run.id}/status").json()

    assert body["done"] is False
    assert [m["clearance"] for m in body["markets"].values()] == ["pending"] * 3


def test_the_board_headline_counts_the_cleared_markets_and_names_the_others(console):
    client, store, _launched, _jobs = console
    run = _judged_run(store)

    body = client.get(f"/runs/{run.id}").text

    assert "CLEARED FOR LAUNCH IN 1 OF 3 MARKETS" in body
    assert re.search(r"FR", body) and re.search(r"SA", body)
    assert "public-dashboards" in body, "the Grafana surface is the board's floor"
    assert f"/runs/{run.id}/panels/timeline.png" in body


def test_the_board_admits_that_the_panels_are_not_there_until_the_publisher_runs(console):
    """A Grafana panel reading "No data" on a board that is still working
    says something false about the run."""
    client, store, _launched, _jobs = console
    run = store.create_run(asset_path=ASSET, markets=list(MARKETS))
    store.set_run_t0(run.id, time.time())

    body = client.get(f"/runs/{run.id}").text

    assert "panels land when the publisher runs" in body.lower()
    assert "/panels/timeline.png" not in body


# -- the mission feed --
#
# The stream is driven as a raw ASGI call rather than through TestClient:
# TestClient runs the whole app to completion before it builds a response
# object (starlette.testclient buffers every http.response.body message into
# one BytesIO), so `client.stream()` on an endpoint that stays open by design
# would simply never return. Driving the app directly is also closer to what
# the browser does: it reads chunks as they are sent and hangs up when it has
# had enough, which is exactly what `stop` does below.


def _drive_sse(path, *, headers=None, on_open=None, want=1, timeout=8.0):
    """Open the SSE route, run `on_open` once the response has started, and
    collect body chunks until `want` data lines have arrived. Returns
    (status, headers, chunks)."""
    async def drive():
        stop = asyncio.Event()
        seen = {"status": None, "headers": {}, "chunks": []}

        async def receive():
            await stop.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.start":
                seen["status"] = message["status"]
                seen["headers"] = {k.decode(): v.decode() for k, v in message["headers"]}
                if on_open is not None:
                    on_open()
            elif message["type"] == "http.response.body":
                body = message.get("body", b"")
                if body:
                    seen["chunks"].append(body.decode())
                if sum(c.count("data:") for c in seen["chunks"]) >= want:
                    stop.set()

        scope = {
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1", "method": "GET", "scheme": "http",
            "path": path, "raw_path": path.encode(), "query_string": b"",
            "root_path": "", "client": ("testclient", 50000),
            "server": ("testserver", 80),
            "headers": [(k.lower().encode(), v.encode())
                        for k, v in (headers or {}).items()],
        }
        await asyncio.wait_for(app_module.app(scope, receive, send), timeout)
        return seen["status"], seen["headers"], seen["chunks"]

    return asyncio.run(drive())


def _sse_events(chunks):
    """Every data: payload in the order it was sent."""
    out = []
    for line in "".join(chunks).splitlines():
        if line.startswith("data:"):
            out.append(json.loads(line[len("data:"):]))
    return out


def test_the_feed_streams_an_event_emitted_after_the_client_connected(console):
    _client, store, _launched, _jobs = console
    run = store.create_run(asset_path=ASSET, markets=list(MARKETS))

    status, headers, chunks = _drive_sse(
        f"/runs/{run.id}/feed",
        on_open=lambda: store.emit(run.id, "analyst", "observe -> shot 7"))

    assert status == 200
    assert headers["content-type"].startswith("text/event-stream")
    assert "retry:" in chunks[0], "an SSE stream must name its reconnect delay"
    events = _sse_events(chunks)
    assert events[-1]["agent"] == "analyst"
    assert events[-1]["message"] == "observe -> shot 7"
    assert events[-1]["id"] > 0, "every event needs an id or resuming is impossible"


def test_the_feed_resumes_from_the_last_event_id_header(console):
    _client, store, _launched, _jobs = console
    run = store.create_run(asset_path=ASSET, markets=list(MARKETS))
    first = store.emit(run.id, "ingest", "detecting shots")
    store.emit(run.id, "analyst", "observe -> shot 0")

    _status, _headers, chunks = _drive_sse(
        f"/runs/{run.id}/feed", headers={"Last-Event-ID": str(first)})

    messages = [event["message"] for event in _sse_events(chunks)]
    assert messages == ["observe -> shot 0"], "an old event was replayed"


def test_the_mission_page_renders_the_backlog_and_the_agent_badges(console):
    client, store, _launched, _jobs = console
    run = _judged_run(store)
    store.emit(run.id, "remediator", "reframe instruction: crop the glass out")

    body = client.get(f"/runs/{run.id}/mission").text

    assert "reframe instruction" in body
    assert "remediator" in body


# -- the market room --

def test_the_market_room_shows_the_guard_block_as_a_human_decision(console):
    client, store, _launched, _jobs = console
    run = _judged_run(store)

    body = client.get(f"/runs/{run.id}/markets/SA").text

    assert "human decision required" in body.lower()
    assert "protected basis: a human decides this one" in body
    assert "SA-LGBT-01" in body


def test_a_market_the_run_never_asked_for_is_404(console):
    client, store, _launched, _jobs = console
    run = _judged_run(store)

    assert client.get(f"/runs/{run.id}/markets/DE").status_code == 404


def test_the_market_room_shows_the_citation_link_and_the_severity(console):
    client, store, _launched, _jobs = console
    run = _judged_run(store)

    body = client.get(f"/runs/{run.id}/markets/FR").text

    assert "FR-ALC-01" in body and "Loi Evin" in body
    assert "https://example.org/evin" in body


# -- manual remediation (the demo affordance; Grafana is the real trigger) --

def test_remediating_an_open_unblocked_finding_enqueues_the_job(console):
    client, store, _launched, jobs = console
    run = _judged_run(store)

    response = client.post(
        f"/runs/{run.id}/findings/fnd_FR_FR-ALC-01_obs_shot_0_000/remediate",
        follow_redirects=False)

    assert response.status_code == 303
    assert jobs == [(run.id, "fnd_FR_FR-ALC-01_obs_shot_0_000", "FR")]


def test_remediating_a_guard_blocked_finding_is_refused(console):
    client, store, _launched, jobs = console
    run = _judged_run(store)

    response = client.post(
        f"/runs/{run.id}/findings/fnd_SA_SA-LGBT-01_obs_shot_6_000/remediate",
        follow_redirects=False)

    assert response.status_code in (404, 409)
    assert jobs == []


def test_remediating_an_unknown_or_already_resolved_finding_is_404(console):
    client, store, _launched, jobs = console
    run = _judged_run(store)
    store.update_finding_status("fnd_FR_FR-LANG-01_obs_shot_1_000", "resolved",
                                run_id=run.id)

    for finding_id in ("fnd_nope", "fnd_FR_FR-LANG-01_obs_shot_1_000"):
        response = client.post(f"/runs/{run.id}/findings/{finding_id}/remediate",
                               follow_redirects=False)
        assert response.status_code == 404, finding_id
    assert jobs == []


# -- artifacts --

def test_a_still_filename_cannot_escape_the_run_directory(console, tmp_path):
    client, store, _launched, _jobs = console
    run = _judged_run(store)
    secret = tmp_path / ".env"
    secret.write_text("GRAFANA_SA_TOKEN=nope")

    for attempt in ("..%2f..%2f.env", "%2e%2e%2f%2e%2e%2f.env", "..%2f.env"):
        response = client.get(f"/runs/{run.id}/stills/{attempt}")
        assert response.status_code == 404, attempt
        assert "nope" not in response.text


def test_media_routes_404_when_the_artifact_is_not_there_yet(console):
    client, store, _launched, _jobs = console
    run = _judged_run(store)

    assert client.get(f"/runs/{run.id}/media/localized/FR").status_code == 404
    assert client.get(f"/runs/{run.id}/media/localized/DE").status_code == 404


def test_the_original_master_is_served_from_the_stores_own_record(console, tmp_path):
    client, store, _launched, _jobs = console
    asset = tmp_path / "ad.mp4"
    asset.write_bytes(b"\x00\x01video")
    run = store.create_run(asset_path=str(asset), markets=list(MARKETS))

    response = client.get(f"/runs/{run.id}/media/original")

    assert response.status_code == 200
    assert response.content == b"\x00\x01video"


# -- the instrument panel --

def test_a_panel_render_is_cached_and_served_without_calling_grafana(console, monkeypatch):
    client, store, _launched, _jobs = console
    run = _judged_run(store)
    cached = app_module.run_dir(run) / "panels" / "clearance.png"
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_bytes(b"\x89PNG cached")
    monkeypatch.setattr(app_module, "_render_panel", _never_call)

    response = client.get(f"/runs/{run.id}/panels/clearance.png")

    assert response.status_code == 200
    assert response.content == b"\x89PNG cached"


def test_a_stale_panel_is_served_when_grafana_will_not_render(console, monkeypatch):
    """An expired panel is worth more than a broken image on the board."""
    client, store, _launched, _jobs = console
    run = _judged_run(store)
    cached = app_module.run_dir(run) / "panels" / "timeline.png"
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_bytes(b"\x89PNG stale")
    old = time.time() - app_module.PANEL_CACHE_S - 60
    os.utime(cached, (old, old))
    monkeypatch.setattr(app_module, "_render_panel", _always_fail)

    response = client.get(f"/runs/{run.id}/panels/timeline.png")

    assert response.status_code == 200
    assert response.content == b"\x89PNG stale"


def test_an_unknown_panel_and_a_run_with_no_clock_are_404(console, monkeypatch):
    client, store, _launched, _jobs = console
    run = _judged_run(store)
    monkeypatch.setattr(app_module, "_render_panel", _never_call)

    assert client.get(f"/runs/{run.id}/panels/nope.png").status_code == 404
    # no t0 means no mapped window, so there is no panel to render yet
    unstarted = store.create_run(asset_path=ASSET, markets=list(MARKETS))
    assert client.get(f"/runs/{unstarted.id}/panels/timeline.png").status_code == 404


def _never_call(run, spec):
    raise AssertionError("Grafana must not be touched for a fresh cached panel")


def _always_fail(run, spec):
    raise RuntimeError("grafana is down")


# -- the cutting room --

def test_the_cutting_room_says_when_no_master_has_been_localized_yet(console):
    client, store, _launched, _jobs = console
    run = _judged_run(store)

    body = client.get(f"/runs/{run.id}/cutting").text

    assert "no localized master" in body.lower()


def test_the_cutting_room_lists_the_change_records_with_both_stills(console, tmp_path):
    client, store, _launched, _jobs = console
    run = _judged_run(store)
    run_dir = app_module.run_dir(run)
    (run_dir / "changes").mkdir(parents=True, exist_ok=True)
    (run_dir / "changes" / "chg_1_before_kf0.png").write_bytes(b"png")
    (run_dir / "changes" / "chg_1_after_kf0.png").write_bytes(b"png")
    (run_dir / "localized_FR.mp4").write_bytes(b"mp4")
    store.add_change(ChangeRecord(
        id="chg_1", run_id=run.id, finding_id="fnd_FR_FR-ALC-01_obs_shot_0_000",
        method="reframe", description="reframe: crop the wine glass out of frame",
        before_frame=str(run_dir / "changes" / "chg_1_before_kf0.png"),
        after_frame=str(run_dir / "changes" / "chg_1_after_kf0.png")))

    body = client.get(f"/runs/{run.id}/cutting").text

    assert "reframe" in body
    assert "chg_1_before_kf0.png" in body and "chg_1_after_kf0.png" in body
    assert f"/runs/{run.id}/media/localized/FR" in body
    assert client.get(f"/runs/{run.id}/stills/chg_1_before_kf0.png").status_code == 200


# -- evidence frames --

def test_the_market_room_links_the_frame_that_triggered_each_finding(client, tmp_path):
    """A finding is a claim about pixels; the room shows the pixels.

    The observation behind the finding recorded its evidence keyframe. The
    market room renders that frame as a thumbnail on the finding, served by
    the evidence route, so a reviewer never has to take a rationale on faith.
    """
    test_client, store, run, _ = client
    frame = tmp_path / "shot_0_kf0.png"
    frame.write_bytes(b"\x89PNG\r\n\x1a\n not a real png but a real file")
    store.add_observations(run.id, [Observation(
        id="obs_shot_0_000", shot_id="shot_0", t_start=0.0, t_end=7.0,
        dimension="alcohol_tobacco_drugs",
        statement="A glass of red wine sits on the table.",
        evidence_frame=str(frame), confidence=0.91,
    )])

    page = test_client.get(f"/runs/{run.id}/markets/FR")
    assert page.status_code == 200
    assert f"/runs/{run.id}/evidence/obs_shot_0_000" in page.text

    img = test_client.get(f"/runs/{run.id}/evidence/obs_shot_0_000")
    assert img.status_code == 200
    assert img.headers["content-type"].startswith("image/")


def test_a_finding_without_a_live_frame_shows_no_thumbnail_and_404s(client):
    """No observation, an empty evidence_frame, or a deleted file: the page
    simply omits the thumbnail and the route answers 404 rather than leaking
    whether the id exists."""
    test_client, store, run, _ = client
    page = test_client.get(f"/runs/{run.id}/markets/FR")
    assert page.status_code == 200
    assert "/evidence/" not in page.text

    assert test_client.get(f"/runs/{run.id}/evidence/obs_shot_0_000").status_code == 404

    store.add_observations(run.id, [Observation(
        id="obs_gone_000", shot_id="shot_1", t_start=7.0, t_end=9.0,
        dimension="text_legibility", statement="A note.",
        evidence_frame="/nonexistent/frame.png", confidence=0.8,
    )])
    assert test_client.get(f"/runs/{run.id}/evidence/obs_gone_000").status_code == 404


# -- style modes --

def test_every_page_offers_the_three_style_modes(client):
    """The console ships three style modes: mission (default), studio and
    screening. The switcher is in the top bar on every page, and the choice
    is applied before first paint so a saved mode never flashes."""
    test_client, _, run, _ = client
    for path in ("/", f"/runs/{run.id}"):
        page = test_client.get(path)
        assert page.status_code == 200
        assert 'data-set-theme="studio"' in page.text
        assert 'data-set-theme="screening"' in page.text
        assert 'data-set-theme="spectrum"' in page.text
        assert "customs-theme" in page.text


# -- the youtube way in --

def test_a_youtube_link_launches_a_clearance(console, monkeypatch, tmp_path):
    """Paste a link, pick markets, and the crew starts on the fetched file."""
    test_client, store, launched, _ = console

    def fake_fetch(url, folder, max_s, max_b):
        target = Path(folder) / "Solstice_Launch_Spot.mp4"
        target.write_bytes(b"video")
        return target

    monkeypatch.setattr(app_module, "fetch_youtube", fake_fetch)
    reply = test_client.post(
        "/runs",
        data={"youtube_url": "https://youtu.be/dQw4w9WgXcQ", "markets": ["FR"]},
        follow_redirects=False)
    assert reply.status_code == 303
    assert len(launched) == 1
    assert launched[0][1].endswith("Solstice_Launch_Spot.mp4")


def test_a_refused_link_is_a_400_with_the_reason(console, monkeypatch):
    test_client, *_ = console
    from customs.fetch import FetchError

    def refusing_fetch(url, folder, max_s, max_b):
        raise FetchError("That video is 300 seconds long. Customs clears "
                         "commercials up to 120 seconds.")

    monkeypatch.setattr(app_module, "fetch_youtube", refusing_fetch)
    reply = test_client.post(
        "/runs",
        data={"youtube_url": "https://youtu.be/dQw4w9WgXcQ", "markets": ["FR"]})
    assert reply.status_code == 400
    assert "300 seconds" in reply.text


def test_neither_file_nor_link_is_a_400(console):
    test_client, *_ = console
    reply = test_client.post("/runs", data={"markets": ["FR"]})
    assert reply.status_code == 400
    assert "upload a file or paste a YouTube link" in reply.text


def test_both_file_and_link_is_a_400(console):
    test_client, *_ = console
    reply = test_client.post(
        "/runs",
        data={"youtube_url": "https://youtu.be/dQw4w9WgXcQ", "markets": ["FR"]},
        files={"asset": ("ad.mp4", b"bytes", "video/mp4")})
    assert reply.status_code == 400
    assert "not both" in reply.text


# -- the archive and the timeline --

def test_the_archive_lists_every_run(client):
    test_client, store, run, _ = client
    other = store.create_run(asset_path=ASSET, markets=["US"])
    page = test_client.get("/runs")
    assert page.status_code == 200
    assert run.id in page.text and other.id in page.text
    assert "All runs" in page.text
    # and home points at it
    assert 'href="/runs"' in test_client.get("/").text


def test_the_timeline_shows_where_it_goes_wrong(client, tmp_path):
    """One lane per market, one segment per finding at its timecode span,
    and the hover card carries the triggering frame and the infraction."""
    test_client, store, run, _ = client
    frame = tmp_path / "kf.png"
    frame.write_bytes(b"\x89PNG fake")
    store.add_observations(run.id, [Observation(
        id="obs_shot_0_000", shot_id="shot_0", t_start=0.0, t_end=7.0,
        dimension="alcohol_tobacco_drugs", statement="Wine on the table.",
        evidence_frame=str(frame), confidence=0.9,
    )])

    page = test_client.get(f"/runs/{run.id}/timeline")
    assert page.status_code == 200
    assert 'class="seg ' in page.text                      # a drawn segment
    assert "FR-ALC-01" in page.text                        # naming its rule
    assert f"/runs/{run.id}/evidence/obs_shot_0_000" in page.text  # the frame
    assert "wine glasses" in page.text                     # the rationale
    # both markets get a lane, findings or not
    assert 'data-lane="FR"' in page.text and 'data-lane="SA"' in page.text

    assert test_client.get("/runs/nope/timeline").status_code == 404


# -- runs survive a deploy --

def test_state_is_mirrored_out_and_restored_into_a_fresh_container(tmp_path, monkeypatch):
    """A deploy replaces the container. What was in the bucket comes back."""
    from customs import persist
    from customs.store import Store

    bucket = tmp_path / "bucket"
    monkeypatch.setenv("CUSTOMS_STATE_DIR", str(bucket))

    old = tmp_path / "old" / "customs.db"
    old.parent.mkdir(parents=True)
    store_a = Store(old)
    run = store_a.create_run(asset_path=str(old.parent / "ad.mp4"), markets=["FR"])
    (old.parent / "uploads").mkdir()
    (old.parent / "uploads" / "ad.mp4").write_bytes(b"master")
    (old.parent / "work" / "audio").mkdir(parents=True)
    (old.parent / "work" / "audio" / "shot_0.wav").write_bytes(b"regenerable")
    (old.parent / "work" / "frames").mkdir(parents=True)
    (old.parent / "work" / "frames" / "shot_0_kf0.png").write_bytes(b"evidence")
    assert "store=True" in persist.snapshot(old)

    fresh = tmp_path / "fresh" / "customs.db"
    fresh.parent.mkdir(parents=True)
    assert "restored store=True" in persist.restore(fresh)
    assert Store(fresh).get_run(run.id) is not None
    assert (fresh.parent / "uploads" / "ad.mp4").read_bytes() == b"master"
    # scratch audio is not worth mirroring, but the evidence frames every
    # finding points at are: a restored run keeps its proof
    assert not (fresh.parent / "work" / "audio").exists()
    assert (fresh.parent / "work" / "frames" / "shot_0_kf0.png").read_bytes() == b"evidence"


def test_restore_never_overwrites_a_container_that_already_has_runs(tmp_path, monkeypatch):
    from customs import persist
    from customs.store import Store

    monkeypatch.setenv("CUSTOMS_STATE_DIR", str(tmp_path / "bucket"))
    old = tmp_path / "old" / "customs.db"; old.parent.mkdir(parents=True)
    Store(old).create_run(asset_path="a.mp4", markets=["FR"])
    persist.snapshot(old)

    live = tmp_path / "live" / "customs.db"; live.parent.mkdir(parents=True)
    keep = Store(live).create_run(asset_path="b.mp4", markets=["SA"])
    assert "kept local store" in persist.restore(live)
    assert Store(live).get_run(keep.id) is not None


def test_without_a_bucket_persistence_is_a_no_op(tmp_path, monkeypatch):
    from customs import persist
    monkeypatch.delenv("CUSTOMS_STATE_DIR", raising=False)
    assert persist.snapshot(tmp_path / "x.db") == "no state dir"
    assert persist.restore(tmp_path / "x.db") == "no state dir"


def test_the_store_is_never_opened_over_the_mount(tmp_path, monkeypatch):
    """SQLite on Cloud Storage FUSE is the trap that made restore silently
    do nothing: the snapshot is built locally and copied across as bytes,
    and the restore is a byte copy back."""
    from customs import persist
    from customs.store import Store

    bucket = tmp_path / "bucket"
    monkeypatch.setenv("CUSTOMS_STATE_DIR", str(bucket))
    live = tmp_path / "live" / "customs.db"
    live.parent.mkdir(parents=True)
    run = Store(live).create_run(asset_path="a.mp4", markets=["BE"])

    opened: list[str] = []
    real_connect = persist.sqlite3.connect

    def spy(target, *args, **kwargs):
        opened.append(str(target))
        return real_connect(target, *args, **kwargs)

    monkeypatch.setattr(persist.sqlite3, "connect", spy)
    assert "store=True" in persist.snapshot(live)
    assert not any(str(bucket) in path for path in opened)
    assert not list(live.parent.glob("*.tmp"))         # the temp copy is cleaned up

    fresh = tmp_path / "fresh" / "customs.db"
    fresh.parent.mkdir(parents=True)
    opened.clear()
    assert "restored store=True" in persist.restore(fresh)
    assert not any(str(bucket) in path for path in opened)
    assert Store(fresh).get_run(run.id) is not None


def test_cleared_never_stands_alone_when_findings_are_still_open(client):
    """"Cleared" means nothing open disqualifies the market, which is not
    the same as nothing being wrong. The room says so in words, and the
    count travels with the verdict."""
    test_client, store, run, _ = client
    store.set_run_status(run.id, "done")     # a verdict needs a finished run
    store.add_findings([_finding(
        run.id, id="fnd_SA_SA-HUM-01_obs_shot_2_000", market="SA",
        rule_id="SA-HUM-01", klass="offence", severity=90,
        rationale="a joke that does not travel", remediable=False)])

    page = test_client.get(f"/runs/{run.id}/markets/SA")
    assert page.status_code == 200
    # an offence finding never blocks, so the market reads cleared...
    assert "Cleared to air, but not clean" in page.text
    assert "s-noted" in page.text
    assert "still open" in page.text


def test_a_market_being_edited_does_not_report_the_verdict_it_has_not_earned(client):
    """clearance() ignores findings at "remediating" so the alert can
    resolve. The tile must not inherit that optimism while the edit runs."""
    test_client, store, run, _ = client
    store.set_run_status(run.id, "done")
    store.update_finding_status("fnd_FR_FR-ALC-01_obs_shot_0_000", "remediating", run.id)

    body = test_client.get(f"/runs/{run.id}/status").json()
    assert body["markets"]["FR"]["clearance"] == "cleared"   # the metric's view
    assert body["markets"]["FR"]["working"] == 1
    board = test_client.get(f"/runs/{run.id}")
    assert "t-pending" in board.text                          # the operator's view
    assert body["markets"]["FR"]["display"] == "pending"


def test_a_running_clearance_reports_how_far_along_it_is(client):
    """There is no counter to read: progress is inferred from what the
    agents have already said they did, which is the only honest source."""
    test_client, store, run, _ = client
    store.set_run_status(run.id, "running")

    first = test_client.get(f"/runs/{run.id}/status").json()["progress"]
    assert first["pct"] < 10 and "detecting" in first["stage"]

    store.emit(run.id, "ingest", "ingest -> 12 raw shot(s) merged to 8")
    for i in range(8):
        store.emit(run.id, "transcription", f"shot_{i} -> 40 char(s)")
    for i in range(4):
        store.emit(run.id, "analyst", f"observe -> shot_{i}")
    half = test_client.get(f"/runs/{run.id}/status").json()["progress"]
    assert 30 < half["pct"] < 70
    assert "shot 4 of 8" in half["stage"]

    store.emit(run.id, "adjudicator", "FR clearance -> blocked (2 finding(s))")
    judging = test_client.get(f"/runs/{run.id}/status").json()["progress"]
    assert judging["pct"] > half["pct"]
    assert "judging markets" in judging["stage"]

    # and a finished run is simply done, whatever the events say
    store.set_run_status(run.id, "done")
    assert test_client.get(f"/runs/{run.id}/status").json()["progress"] == {
        "pct": 100, "stage": "done"}


def test_the_frame_board_puts_every_frame_beside_what_was_read_from_it(client, tmp_path):
    test_client, store, run, _ = client
    frame = tmp_path / "kf.png"
    frame.write_bytes(b"\x89PNG frame")
    store.add_observations(run.id, [
        Observation(id="obs_shot_0_000", shot_id="shot_0", t_start=0.0, t_end=7.0,
                    dimension="alcohol_tobacco_drugs",
                    statement="A glass of red wine sits on the table.",
                    evidence_frame=str(frame), confidence=0.91),
        Observation(id="obs_shot_1_000", shot_id="shot_1", t_start=7.0, t_end=9.0,
                    dimension="gender_portrayal", statement="A woman walks past.",
                    evidence_frame="", confidence=0.6),
    ])

    page = test_client.get(f"/runs/{run.id}/frames")
    assert page.status_code == 200
    # the frame, the analyst's neutral sentence, and the finding hung off it
    assert f"/runs/{run.id}/evidence/obs_shot_0_000" in page.text
    assert "A glass of red wine sits on the table." in page.text
    assert "FR-ALC-01" in page.text
    # an observation nobody objected to says so rather than looking flagged
    assert "no market objected to this" in page.text
    # and one whose frame is gone still shows its reading
    assert "A woman walks past." in page.text
    assert "no frame kept" in page.text

    assert test_client.get("/runs/nope/frames").status_code == 404


def test_agent_mode_talks_to_vertex_on_the_endpoint_that_has_the_models(monkeypatch):
    """The models this project can reach are on Vertex's global endpoint,
    which is why genai_client pins it. ADK builds its own client from the
    environment, so it has to be told the same thing: deploying with
    GOOGLE_CLOUD_LOCATION=europe-west1 had the agent asking a region that
    does not carry the model."""
    from customs import agentmode

    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "europe-west1")
    agentmode._vertex_env()
    assert os.environ["GOOGLE_CLOUD_LOCATION"] == "global"
    assert os.environ["GOOGLE_GENAI_USE_VERTEXAI"] == "true"


def test_the_run_nav_puts_each_jurisdiction_level_on_its_own_labelled_row(client):
    """A run can cover a baseline, a continent, countries and broadcasters at
    once; one flat strip of codes hides which is which."""
    test_client, store, _run, _ = client
    run = store.create_run(asset_path=ASSET,
                           markets=["GLOBAL", "EU", "FR", "BE", "BE-VRT", "FR-M6"])
    page = test_client.get(f"/runs/{run.id}")
    assert page.status_code == 200
    for level in ("global", "continental", "national", "channel"):
        assert f'<span class="lvl-tag label">{level}</span>' in page.text
    # and the rows carry the right members
    body = page.text
    channel_row = body.split('>channel</span>')[1][:400]
    assert "BE-VRT" in channel_row and "FR-M6" in channel_row
    assert "GLOBAL" not in channel_row


def test_the_generated_seconds_are_downloadable(client, tmp_path):
    """A bridge invents footage. The master is what ships, but the raw clip
    is the part nobody shot, so it is kept beside the change record's stills
    and served rather than left in a scratch directory a deploy will wipe."""
    test_client, store, run, _ = client
    changes = app_module.run_dir(run) / "changes"
    changes.mkdir(parents=True, exist_ok=True)
    (changes / "chg_abc123_bridge.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42")

    got = test_client.get(f"/runs/{run.id}/changes/chg_abc123/generated.mp4")
    assert got.status_code == 200
    assert got.headers["content-type"] == "video/mp4"

    # a change with no generated footage, and a forged id, answer the same way
    assert test_client.get(
        f"/runs/{run.id}/changes/chg_ffffff/generated.mp4").status_code == 404
    assert test_client.get(
        f"/runs/{run.id}/changes/not-an-id/generated.mp4").status_code == 404
