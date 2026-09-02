"""Alert webhook tests, offline.

The store is pointed at a tmp database and the remediation job is replaced by
a recorder, so these tests are about exactly one thing: which alerts turn
into work, and which are dropped. TestClient runs a BackgroundTask after the
response has been returned, so a recorded call proves the task was really
enqueued rather than just planned.
"""
import sqlite3

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
    monkeypatch.setattr(app_module.remediate, "plan", lambda finding, observation=None: "prop_swap")
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
    # observations as well as findings: a finding says a market objected,
    # an observation says what was seen and when, and the lane chart is
    # built from both
    store.add_observations(run.id, [
        Observation(id="obs_shot_0_000", shot_id="shot_0", t_start=1.0, t_end=2.0,
                    dimension="alcohol_tobacco_drugs", statement="A wine glass.",
                    evidence_frame="/x/a.png", confidence=0.9),
        Observation(id="obs_shot_1_000", shot_id="shot_1", t_start=4.2, t_end=5.1,
                    dimension="text_legibility", statement="English on a cup.",
                    evidence_frame="/x/b.png", confidence=0.95),
        Observation(id="obs_shot_2_000", shot_id="shot_2", t_start=7.5, t_end=8.0,
                    dimension="gesture_body_language", statement="A thumbs up.",
                    evidence_frame="/x/c.png", confidence=0.88),
    ])
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

def test_home_offers_every_market_pack_and_leaves_history_to_its_own_tab(console):
    client, _store, _launched, _jobs = console

    body = client.get("/new").text

    assert client.get("/new").status_code == 200
    for market in MARKETS:
        assert f'value="{market}"' in body, market
    # Starting a clearance and reading the ones already done are different
    # jobs. The form used to carry the last twelve runs underneath it, which
    # put the history a scroll below a form nobody was filling in.
    assert "no runs yet" not in body.lower()
    assert 'href="/runs"' in body  # ...but the tab that has it is one click away

    history = client.get("/runs")
    assert history.status_code == 200
    assert "no runs yet" in history.text.lower()


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
        "working": 0, "resolved": 0, "blocked": 0, "errored": False,
        # which KINDS of problem, worst severity first -- the tile draws
        # these as the taxonomy's own icons, so a count is no longer the
        # only thing it can say about a market
        "kinds": ["alcohol_tobacco_drugs", "text_legibility"]}
    assert body["markets"]["SA"]["errored"] is True
    assert body["markets"]["SA"]["blocked"] == 1
    assert body["markets"]["US"]["clearance"] == "cleared"
    assert body["overall"]["cleared"] == 1
    assert body["overall"]["total"] == 3
    assert body["overall"]["failing"] == ["FR", "SA"]


def test_a_run_that_died_before_adjudication_is_not_go_for_launch(console):
    """A corrupt or audio-only upload passes the door checks and then dies in
    ingest: run status "error", zero findings anywhere, no adjudicator event.
    clearance([]) says "cleared" for such a market, so this exact run used to
    render GO FOR LAUNCH with every tile green and progress at "done" -- the
    single most dishonest thing the board could do, and reachable by any
    judge with a broken file."""
    client, store, _launched, _jobs = console
    run = store.create_run(asset_path=ASSET, markets=list(MARKETS))
    store.set_run_t0(run.id, time.time())
    store.emit(run.id, "pipeline", "stage_error: run: ffmpeg exited 234")
    store.set_run_status(run.id, "error")

    body = client.get(f"/runs/{run.id}/status").json()

    assert all(m["errored"] for m in body["markets"].values())
    assert body["overall"]["state"] == "no_go"
    assert body["overall"]["cleared"] == 0
    assert body["progress"]["stage"] == "stopped on an error"


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
    store.emit(run.id, "remediator", "prop_swap instruction: swap the glass for a teacup")

    body = client.get(f"/runs/{run.id}/mission").text

    assert "prop_swap instruction" in body
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
        method="prop_swap", description="prop_swap: the wine glass becomes a teacup",
        before_frame=str(run_dir / "changes" / "chg_1_before_kf0.png"),
        after_frame=str(run_dir / "changes" / "chg_1_after_kf0.png")))

    body = client.get(f"/runs/{run.id}/cutting").text

    assert "prop_swap" in body
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

def test_every_page_offers_both_style_modes(client):
    """The console ships two style modes: mission (default) and studio.

    It shipped four. Screening and Spectrum were removed: nobody switched
    between four house styles, and each one was another place every new
    colour had to be defined or silently go missing.
    """
    test_client, _, run, _ = client
    for path in ("/", f"/runs/{run.id}"):
        page = test_client.get(path)
        assert page.status_code == 200
        assert 'data-set-theme=""' in page.text      # mission
        assert 'data-set-theme="studio"' in page.text
        assert 'data-set-theme="screening"' not in page.text
        assert 'data-set-theme="spectrum"' not in page.text
        # applied before first paint, so a saved mode never flashes
        assert "customs-theme" in page.text


def test_studio_is_the_default_everywhere_and_mission_must_be_asked_for(client):
    """The console is read in daylight, printed, screenshotted and put in
    front of people, so the light theme is the default on every screen size
    -- not just phones, which is what it used to be.

    The half that is easy to get wrong: an explicit Mission has to be stored
    AS "mission". If it were stored as a missing key, it would be
    indistinguishable from never having chosen, and the next page load would
    silently revert to Studio.
    """
    test_client, _, _run, _ = client
    head = test_client.get("/").text.split("</head>")[0]
    assert '_t !== "mission"' in head, "anything but an explicit mission is light"
    assert 'setAttribute("data-theme", "studio")' in head
    assert '_t !== "studio" && _t !== "mission"' in head

    # scoped to the theme switch: the card/list view switches have their own
    # KEY in their own closure and legitimately still remove it
    js = test_client.get("/static/customs.js").text
    theme_block = js.split('var KEY = "customs-theme";')[1].split("})();")[0]
    assert 'localStorage.setItem(KEY, mode || "mission")' in theme_block
    assert "removeItem" not in theme_block


def test_a_browser_holding_a_deleted_mode_falls_back_to_studio(client):
    """A stored "spectrum" would otherwise set data-theme with no CSS behind
    it, leaving that browser on a half-styled page nothing could undo: the
    button that cleared it is gone. The boot script drops anything it does
    not recognise and lands on Studio, the default."""
    test_client, _, _run, _ = client
    head = test_client.get("/").text.split("</head>")[0]
    assert 'localStorage.removeItem("customs-theme")' in head
    assert 'setAttribute("data-theme", "studio")' in head


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
    """The page is one grid now, not three charts: occurrence types down the
    side, the scene's own opening frame across the top, and a cell wherever
    the two meet. The market Gantt it replaced said the same things one
    market at a time, and every one of them is still in the market rooms."""
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
    assert 'class="mg"' in page.text                       # the grid
    assert "alcohol tobacco drugs" in page.text            # its y axis
    assert f"/runs/{run.id}/evidence/obs_shot_0_000" in page.text  # its x axis
    assert "wine glasses" in page.text                     # the rationale
    assert "FR-ALC-01 (FR)" in page.text                   # naming its rule
    # the market is in the cell that fired, not in a lane of its own: a
    # market with nothing to say cost a whole empty row in the old Gantt
    assert 'data-lane=' not in page.text

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

    # a second observation in the SAME shot: the board must not repeat the
    # scene as another full-width row of near-identical frames
    store.add_observations(run.id, [
        Observation(id="obs_shot_0_001", shot_id="shot_0", t_start=3.0, t_end=7.0,
                    dimension="gesture_body_language",
                    statement="A hand raises the glass in a toast.",
                    evidence_frame=str(frame), confidence=0.8),
    ])

    page = test_client.get(f"/runs/{run.id}/frames")
    assert page.status_code == 200
    # the frame, the analyst's neutral sentence, and the finding hung off it
    assert f"/runs/{run.id}/evidence/obs_shot_0_000" in page.text
    assert "A glass of red wine sits on the table." in page.text
    assert "FR-ALC-01" in page.text
    # a scene nobody objected to says so rather than looking flagged
    assert "no market objected to this" in page.text
    # and one whose frame is gone still shows its reading
    assert "A woman walks past." in page.text
    assert "no frame kept" in page.text
    # clustered: two observations of shot_0 share ONE scene card, so the
    # page holds two cards, and the toast sentence sits inside the first
    assert page.text.count('class="sc ') + page.text.count('class="sc"') == 2
    assert "A hand raises the glass in a toast." in page.text
    # same still at both ends of the scene collapses to one image
    assert page.text.count(f"/runs/{run.id}/evidence/obs_shot_0_000") >= 1
    assert f"/runs/{run.id}/evidence/obs_shot_0_001" not in page.text
    # the scenescroll: one opening still per scene, anchor-jumping to its
    # card, with the flagged scene wearing its finding count
    assert 'href="#sc-shot_0"' in page.text and 'id="sc-shot_0"' in page.text
    assert 'href="#sc-shot_1"' in page.text
    assert 'class="ss hit"' in page.text

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


def test_the_board_ticker_carries_the_newest_thing_an_agent_said(console):
    """Under the bar, what is happening right now.

    The percentage barely moves while the analyst reads one shot, so the bar
    alone reads as a hang. The ticker is what says the run is alive.
    """
    client, store, _launched, _jobs = console
    run = store.create_run(asset_path="/x/ad.mp4", markets=["FR"])
    store.emit(run.id, "ingest", "watching the film end to end, hunting for the cuts")
    store.emit(run.id, "analyst", "reading shot_3 the way a regulator would")

    body = client.get(f"/runs/{run.id}/status").json()
    assert body["ticker"]["agent"] == "analyst"
    assert body["ticker"]["message"] == "reading shot_3 the way a regulator would"

    # the id is what the page compares against, so it must move with the feed
    first = body["ticker"]["id"]
    store.emit(run.id, "adjudicator", "FR clearance -> blocked")
    assert client.get(f"/runs/{run.id}/status").json()["ticker"]["id"] > first

    # a run nobody has said anything about tickers nothing, rather than 500ing
    quiet = store.create_run(asset_path="/x/b.mp4", markets=["FR"])
    assert client.get(f"/runs/{quiet.id}/status").json()["ticker"] is None


def test_agent_messages_are_escaped_before_they_reach_the_ticker(client):
    """Event text is model-written and carries file paths. It is data."""
    test_client, _, _run, _ = client
    js = test_client.get("/static/customs.js").text
    assert "escapeHtml(data.ticker.message)" in js
    assert "d.textContent = text" in js  # escapeHtml goes through textContent


# -- restore has to fail loudly, or not at all --

def test_a_torn_restore_leaves_nothing_rather_than_half_a_store(tmp_path, monkeypatch):
    """A copy that dies partway must not become the live store.

    Cloud Run mounts the bucket, so the object under an open read handle can
    be replaced mid-copy: during a rollout the outgoing revision snapshots
    while the incoming one restores, and GCS FUSE raises ESTALE. On
    2026-08-25 that cost a revision all fourteen of its runs, silently,
    because the error was swallowed and an empty store looks exactly like a
    first boot.

    A half-written file is the worse failure: restore() treats any non-empty
    local database as a live store and would never try again.
    """
    from customs import persist
    src, live = tmp_path / "bucket.db", tmp_path / "live.db"
    Store(src).create_run(asset_path="/x/a.mp4", markets=["FR"])
    with sqlite3.connect(src) as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    calls = []
    def torn(a, b, *args, **kwargs):
        calls.append(1)
        Path(b).write_bytes(Path(a).read_bytes()[:64])  # the first page and no more
        return b
    monkeypatch.setattr(persist.shutil, "copy2", torn)
    monkeypatch.setattr(persist.time, "sleep", lambda s: None)

    notes = []
    assert persist._restore_db(src, live, on_note=notes.append) is False
    assert not live.exists(), "a truncated store must not be left on disk"
    assert len(calls) == persist._RESTORE_TRIES  # it really did retry
    assert notes and "did not read back as a store" in notes[0]


def test_a_restore_that_fails_the_first_time_still_gets_its_runs(tmp_path, monkeypatch):
    """ESTALE is transient: the writer finishes and the next read is fine."""
    from customs import persist
    src, live = tmp_path / "bucket.db", tmp_path / "live.db"
    Store(src).create_run(asset_path="/x/a.mp4", markets=["FR"])

    # flush the WAL: the file on disk is the thing being copied, and until
    # it is checkpointed it genuinely is not a complete store yet
    with sqlite3.connect(src) as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    real, attempts = persist.shutil.copy2, []
    def flaky(a, b, *args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise OSError(116, "Stale file handle")
        return real(a, b)
    monkeypatch.setattr(persist.shutil, "copy2", flaky)
    monkeypatch.setattr(persist.time, "sleep", lambda s: None)

    notes = []
    assert persist._restore_db(src, live, on_note=notes.append) is True
    assert len(Store(live).recent_runs(5)) == 1
    assert "Stale file handle" in notes[0]  # and it said so


def test_the_mission_feed_has_a_second_tab_for_what_came_out_of_the_run(console):
    """The feed says what is happening; nothing said what it produced.

    Checking a generated clip meant knowing the change id and typing a URL,
    and the two frames handed to Veo were not kept at all -- they were
    written to the scratch workdir, which is never mirrored, so they went
    with the container. Those two frames are the entire brief Veo was
    given: without them there is no way to tell whether it invented
    something or was handed it.
    """
    client, store, _launched, _jobs = console
    run = store.create_run(asset_path="/x/ad.mp4", markets=["SA"])

    body = client.get(f"/runs/{run.id}/mission").text
    assert 'data-mtab="live"' in body and 'data-mtab="made"' in body
    assert "Nothing generated yet" in body      # honest empty state

    changes = Path(app_module.run_dir(run)) / "changes"
    changes.mkdir(parents=True, exist_ok=True)
    change = ChangeRecord(id="chg_abc123", run_id=run.id, finding_id="fnd_x",
                          method="bridge", description="regenerated 4s",
                          before_frame=str(changes / "chg_abc123_before.png"),
                          after_frame=str(changes / "chg_abc123_after.png"))
    store.add_change(change)
    for name in ("chg_abc123_anchor_head.png", "chg_abc123_anchor_tail.png",
                 "chg_abc123_before.png", "chg_abc123_after.png"):
        (changes / name).write_bytes(b"\x89PNG\r\n\x1a\n")
    (changes / "chg_abc123_bridge.mp4").write_bytes(b"\x00")

    body = client.get(f"/runs/{run.id}/mission").text
    assert "Nothing generated yet" not in body
    assert "chg_abc123_anchor_head.png" in body, "the first frame Veo was given"
    assert "chg_abc123_anchor_tail.png" in body, "and the last"
    assert f"/runs/{run.id}/changes/chg_abc123/generated.mp4" in body
    # and the frames are servable through the existing stills route
    assert client.get(f"/runs/{run.id}/stills/chg_abc123_anchor_head.png").status_code == 200


def test_recent_runs_opens_as_cards_and_remembers_a_choice_of_list(client):
    """Cards by default, and an explicit List has to survive a reload.

    List used to be stored as the ABSENCE of a key. With cards now the
    default, absence means "has not chosen", so removing the key on a List
    click would revert to cards on the next page load -- the same trap the
    style switch had.
    """
    test_client, _, _run, _ = client
    body = test_client.get("/runs").text
    assert 'class="runlist" id="runlist"' in body, "no as-rows: cards by default"
    assert 'data-set-runs="list"' in body, "list stores a real value"
    assert 'data-set-runs=""' not in body

    js = test_client.get("/static/customs.js").text
    block = js.split('var KEY = "customs-runs-view";')[1].split("})();")[0]
    assert 'var saved = "cards"' in block
    assert 'localStorage.setItem(KEY, view)' in block
    assert "removeItem(KEY)" not in block.split("buttons.forEach")[-1]


def test_studio_mode_lands_on_recent_runs(client):
    """Entering the studio almost always means looking at work already done.

    The switch used to point at the upload form, so leaving agent mode
    dropped you on an empty form rather than on your runs. Starting a
    clearance is still one tab away.
    """
    test_client, _, run, _ = client
    for path in ("/", "/agent", f"/runs/{run.id}"):
        body = test_client.get(path).text
        switch = body.split('class="mode-switch"')[1].split("</span>")[0]
        assert 'href="/runs"' in switch, path
        assert 'href="/agent"' in switch, path


def test_the_agent_can_discover_the_schema_and_query_it(client):
    """It must be able to answer a question nobody wrote a grouping for.

    The old surface was a fixed menu: group findings by one of five
    labels. Anything else -- "what do we see that nobody objects to",
    "which dimension has the highest flag rate" -- was unanswerable.

    The guardrail against a model inventing metrics is not hope, it is
    data_schema: the label set and body fields are described, and query
    reports back when an expression returns nothing so the agent can fix
    it rather than assert an empty answer.
    """
    from customs import agentmode
    import inspect
    src = inspect.getsource(agentmode)
    assert "def data_schema()" in src and "def query(" in src
    assert "data_schema, query, build_dashboard" in src, "both must be registered"
    # the schema has to name the observation stream, which is the new half
    assert 'kind="observation"' in src
    assert "flagged" in src and "no rows" in src


def test_a_run_card_draws_its_own_lanes_when_there_is_no_viewer(client):
    """Grafana Cloud sends frame-ancestors 'none', so a card cannot embed
    IT. Where the viewer is deployed the card frames that instead; where it
    is not, the numbers still come from Mimir and the drawing happens here,
    as inline SVG in the product's own hex.

    The severity sparkline that used to sit above the lanes is gone: two
    charts on one card said the same thing twice.
    """
    test_client, _, run, _ = client
    body = test_client.get("/runs").text
    assert 'class="runspark"' not in body, "one chart per card, not two"
    assert 'class="cardlanes"' in body and "/lanes.svg" in body
    assert "onerror=\"this.remove()\"" in body, "a run with no series just has no chart"
    # and with no viewer configured, nothing tries to iframe Grafana
    assert "<iframe" not in body


def test_one_palette_governs_the_app_and_grafana(client):
    """A threshold cannot mean amber here and red in a panel."""
    from customs import state
    assert state.BLOCKED == "#EA4335" and state.CLEARED == "#34A853"
    assert state.AT_RISK == "#FBBC05" and state.SIGNAL == "#4285F4"
    steps = state.grafana_thresholds()["steps"]
    assert [s["color"] for s in steps] == [state.CLEARED, state.AT_RISK, state.BLOCKED]
    # the display bands must not contradict the adjudicator's blocking line
    from customs import adjudicate
    assert state.SEVERITY_BLOCKS == adjudicate.CLEARANCE_SEVERITY_THRESHOLD
    assert state.colour_for_severity(95) == state.BLOCKED
    assert state.colour_for_severity(50) == state.AT_RISK
    assert state.colour_for_severity(10) == state.CLEARED


def test_a_market_tile_charts_its_own_risk(client):
    """The tile and its chart must agree, because they share the thresholds.

    A market peaking at 85 draws red, at 60 amber, because
    spark.sparkline colours by state.colour_for_severity -- the same
    function the pill uses. They cannot drift.
    """
    from customs import spark, state
    red = spark.sparkline([(0, 85), (1, 85)])
    amber = spark.sparkline([(0, 60), (1, 60)])
    green = spark.sparkline([(0, 10), (1, 10)])
    assert state.BLOCKED in red and state.AT_RISK in amber and state.CLEARED in green
    # an empty series draws nothing rather than a flat line at zero
    assert spark.sparkline([]) == ""

    test_client, _, run, _ = client
    body = test_client.get(f"/runs/{run.id}").text
    assert 'class="tilespark"' in body and "/spark.svg" in body
    assert "<iframe" not in body


def test_finding_queries_cannot_be_inflated_by_observation_lines():
    """The kind label was added to findings AFTER 471 of them were pushed.

    Those 471 carry no kind at all, so {app="customs", kind="finding"}
    returns zero against the real history -- while bare {app="customs"}
    now returns findings AND observations together, silently inflating
    every finding count by the observation total. Both are wrong.

    Loki treats an absent label as empty, so the negative matcher is the
    only selector that partitions correctly across the history.
    """
    import json, pathlib
    for path in pathlib.Path("grafana/dashboards").glob("*.json"):
        raw = path.read_text()
        assert '{app=\\"customs\\"}' not in raw, f"{path.name} counts observations as findings"
        assert '{app=\\"customs\\", asset' not in raw, f"{path.name} unscoped"
        json.loads(raw)          # and it is still valid JSON after the edit

    from customs import agentmode
    spec = agentmode.dashboard_spec("", "", "market")
    expr = spec["panels"][0]["targets"][0]["expr"]
    assert 'kind!="observation"' in expr, "the ad-hoc builder must scope too"
    assert 'kind="finding"' not in expr, "that selector misses all of the history"


def test_a_cleared_market_still_shows_something(client):
    """The first version drew nothing on most of the board.

    A bare sparkline of an all-zero series puts every point at the floor
    of the viewBox, so the path sat ON the bottom border with half its
    stroke clipped outside it. Cleared markets are most of a board, so
    most tiles looked empty -- which is what they looked like.

    A stat card cannot fail that way: the NUMBER carries it and the trend
    is context behind. "0 findings" is a result; a blank tile is a bug.
    """
    import re
    from customs import spark, state
    flat = [(i, 0.0) for i in range(20)]
    svg = spark.statcard(flat, value="0", label="FINDINGS",
                         colour=state.CLEARED, width=260, height=76)
    assert ">0</text>" in svg and "FINDINGS" in svg, "the number must be drawn"
    ys = [float(y) for _, y in re.findall(r"[ML](\d+\.\d+),(\d+\.\d+)", svg)]
    shift = float(re.search(r"translate\(0,([\d.]+)\)", svg).group(1))
    assert max(ys) + shift <= 76 - 2, "the flat line must clear the bottom edge"
    assert state.CLEARED in svg

    # and a market with findings reads differently, in its own colour
    hot = [(i, 0.0 if i < 6 else 85.0) for i in range(20)]
    loud = spark.statcard(hot, value="85", label="PEAK SEVERITY",
                          colour=state.BLOCKED, width=260, height=76)
    assert state.BLOCKED in loud and ">85</text>" in loud
    hot_ys = [float(y) for _, y in re.findall(r"[ML](\d+\.\d+),(\d+\.\d+)", loud)]
    assert max(hot_ys) - min(hot_ys) > 20, "a real profile must actually move"


def test_every_chart_is_a_valid_standalone_svg_document():
    """These are served as files and loaded through <img src>, not inlined.

    Inline in a page the SVG namespace is implied, so a missing xmlns is
    invisible to any test that only reads the string. Served as its own
    file it is invalid SVG: the browser fetches it (HTTP 200), fails to
    decode it, fires onerror, and the img tag removes itself. Every market
    tile downloaded its card and then deleted it, which looked exactly
    like the feature never shipping.

    Parsing with a real XML parser is the check that catches it, because
    that is what a browser does.
    """
    import xml.etree.ElementTree as ET
    from customs import spark

    points = [(float(i), float(i * 7 % 100)) for i in range(20)]
    charts = {
        "sparkline": spark.sparkline(points),
        "statcard": spark.statcard(points, value="85", label="PEAK SEVERITY"),
        "bars": spark.bars([("a", 3.0), ("b", 7.0)]),
    }
    for name, svg in charts.items():
        root = ET.fromstring(svg)          # raises on anything malformed
        assert root.tag == "{http://www.w3.org/2000/svg}svg", \
            f"{name} has no SVG namespace, so <img> will refuse it"
        # and an intrinsic size, so it never depends on CSS to know how big it is
        assert root.get("width") and root.get("height"), f"{name} has no intrinsic size"
        assert root.get("viewBox"), f"{name} has no viewBox"


def test_a_tile_says_what_kind_of_problem_not_only_how_many(console):
    """A count tells you a market is unhappy, not what about.

    The kinds are drawn as the taxonomy's own 18 dimension icons -- the
    same glyphs the frame board and the library use -- so one symbol
    means one thing everywhere in the product. Worst severity first, so
    the tile leads with what matters, and deduplicated: a market
    objecting three times over dress is one kind of problem.
    """
    client, store, _launched, _jobs = console
    run = _judged_run(store)
    body = client.get(f"/runs/{run.id}").text
    assert 'class="tile-kinds"' in body
    assert 'href="#d-' in body, "the icons come from the dimension taxonomy"
    # and every icon it asks for must actually exist in the sprite sheet
    import re, pathlib
    sprite = pathlib.Path("src/customs/templates/base.html").read_text()
    for kind in set(re.findall(r'href="#(d-[a-z_]+)"', body)):
        assert f'id="{kind}"' in sprite, f"{kind} is not in the icon set"



def test_the_agent_answer_is_rendered_in_the_consoles_own_language():
    """A reply naming a market, a rule and a timecode used to arrive as
    one grey line, while three feet away those same three things are a
    country chip, a legal chip and a mono timecode.

    The formatter recognises them and renders them as the interface
    already does, so a sentence and a tile speak one dialect.
    """
    from customs import replyfmt, state
    sample = (
        "Two markets are currently blocked:\n\n"
        "* **AE-ABUDHABI** is blocked by rule `AE-MOD-01` at 4.2 - 5.1s "
        "for modesty_dress_body, severity 85.\n"
        "* **ID** is blocked by `ID-MOD-01` at 00:04.2.\n"
    )
    out = replyfmt.render(sample, markets={"AE-ABUDHABI", "ID"},
                          rules={"AE-MOD-01": "legal", "ID-MOD-01": "legal"})
    assert "<ul>" in out and out.count("<li>") == 2, "the list becomes a list"
    assert "<strong>" in out
    assert 'class="chip inl rule"' in out, "a rule id carries its class icon"
    assert 'class="chip inl mkt"' in out, "a market carries its own icon"
    assert 'class="chip inl dim"' in out and "d-modesty_dress_body" in out
    assert 'class="tc-inl mono"' in out, "a timecode is set in mono"
    assert state.BLOCKED in out, "severity 85 is coloured by the shared threshold"
    assert "<code><span" not in out, "a chip must not nest inside a code span"


def test_nothing_the_model_writes_can_become_markup():
    """The reply is escaped BEFORE any markup is inserted, and the entity
    pass only ever runs over already-escaped text. A model can have its
    words recognised as one of our chips; it can never inject an element.
    """
    from customs import replyfmt
    hostile = (
        "<img src=x onerror=alert(1)>\n"
        "* <script>alert(2)</script> and **<b>bold</b>**\n"
        "[a link](javascript:alert(3))\n"
    )
    out = replyfmt.render(hostile, markets=set(), rules={})
    # The words survive -- they are shown as text -- but never as an
    # element or an attribute. That distinction is the whole point: the
    # danger is a tag opening, not the letters "onerror".
    for opener in ("<img", "<script", "<b>", "<a "):
        assert opener not in out, f"{opener!r} became real markup"
    assert "&lt;img src=x onerror=alert(1)&gt;" in out, "shown as inert text"
    assert "&lt;script&gt;" in out
    assert "href=" not in out, "a markdown link must not become an anchor"
    # our own markup is still produced around it
    assert "<strong>" in out and "<li>" in out


def test_the_lane_chart_says_when_each_kind_of_problem_happens(console):
    """The board says which markets are unhappy; the frame board says what
    the analyst saw. Neither said WHEN -- whether a run has one bad shot
    or trouble all the way through.

    A faint dot matters as much as a loud one: a lane thick with pale
    dots and one red one says "we look at this constantly and it is
    almost always fine", which is the negative space findings alone can
    never show.
    """
    test_client, store, _launched, _jobs = console
    run = _judged_run(store)
    body = test_client.get(f"/runs/{run.id}").text

    assert "/lanes.png" in body, "the board asks for the chart as its own URL"

    # and the chart itself, fetched, is a real lane chart. The board asks
    # Grafana to draw it; with no Grafana reachable here that falls back to
    # the SVG, which is the same query and the same palette.
    svg = test_client.get(f"/runs/{run.id}/lanes.svg?full=1")
    assert svg.status_code == 200 and svg.headers["content-type"] == "image/svg+xml"
    assert svg.text.count("lane-dot") > 1
    assert 'href="#d-' in svg.text, "each lane is labelled with its taxonomy icon"


def test_the_lane_chart_is_a_grafana_panel_served_as_an_image(console):
    """Once the hover requirement was dropped, this could become a real
    Grafana panel: a state timeline in grafana/dashboards/lanes.json,
    coloured by the app's own thresholds.

    It is still served as an image rather than an iframe, and that is not
    a choice -- this stack refuses framing with BOTH x-frame-options:
    deny and CSP frame-ancestors 'none', verified in a browser, and the
    admin settings API answers 403 when asked to relax it.

    It is also served as its own URL rather than built into the page.
    Doing it inline meant a Loki round trip per run before a byte went
    out, which took the archive past a two minute timeout and the board
    to thirty-six seconds.
    """
    import json, pathlib
    client, store, _launched, _jobs = console
    run = _judged_run(store)

    body = client.get(f"/runs/{run.id}").text
    assert "<iframe" not in body, "this stack refuses to be framed"
    assert "/lanes.png" in body, "the board asks Grafana for the picture"
    # The chip links to the PUBLIC timeline, not /d/customs-lanes: the lanes
    # dashboard lives on the stack and greets anyone without a Grafana login
    # with a sign-in wall, which is the worst answer to "open in Grafana".
    from customs.config import settings as _settings
    assert _settings.grafana_public_timeline in body, \
        "and links to a Grafana page that opens without a login"
    assert "/d/customs-lanes" not in body, \
        "the login-walled operator dashboard stays out of the chips"

    # The render is Grafana's. When Grafana cannot be reached -- as here --
    # the route falls back to the app-drawn SVG rather than to a broken
    # image: an outage should cost the chart its provenance, not the chart.
    shot = client.get(f"/runs/{run.id}/lanes.png", follow_redirects=False)
    assert shot.status_code in (200, 302)
    if shot.status_code == 302:
        assert shot.headers["location"].endswith("/lanes.svg?full=1")
    else:
        assert shot.headers["content-type"] == "image/png"

    # The card strip stays app-drawn: Grafana Cloud's renderer floors a
    # panel at 1000x500, twice the height a card gives it.
    assert "/lanes.svg" in client.get("/runs").text, "cards keep the strip"

    dash = json.loads(pathlib.Path("grafana/dashboards/lanes.json").read_text())
    panel = dash["panels"][0]
    assert panel["type"] == "state-timeline"
    assert panel["datasource"]["uid"] == "grafanacloud-logs"
    # the app's bands, so a lane cannot be amber here and red on the tile
    from customs import state
    steps = panel["fieldConfig"]["defaults"]["thresholds"]["steps"]
    assert [x["color"] for x in steps] == [state.CLEARED, state.AT_RISK, state.BLOCKED]
    # $__interval over a film's eighty seconds is milliseconds wide, which
    # painted every observation as an invisible hairline. A film is watched
    # in shots, so the bucket has a floor.
    assert panel["targets"][0]["interval"] == "4s"


def test_recent_runs_opens_on_a_banner_made_of_the_products_own_icons(client):
    """The archive is the page people land on, and it opened on a bare
    headline. The banner is the one place in the console allowed to be a
    picture of itself -- and it earns that by being built from the same
    glyphs everything else is labelled with, so it reads as THIS
    product's front page and could not be any other product's.
    """
    import re, pathlib
    test_client, _, _run, _ = client
    body = test_client.get("/runs").text

    assert 'class="runhero"' in body
    assert body.count('class="swarm-row"') == 3, "three rows drifting at different speeds"
    # every symbol the product owns should be in there, not a chosen few
    sprite = pathlib.Path("src/customs/templates/base.html").read_text()
    owned = set(re.findall(r'id="([idn]-[a-z_0-9]+)"', sprite))
    used = set(re.findall(r'<use href="#([idn]-[a-z_0-9]+)"/>', body))
    assert len(used & owned) >= 40, f"only {len(used & owned)} of {len(owned)} icons used"
    # and nothing referenced that does not exist, which would render blank
    assert used <= owned, f"banner asks for icons that are not in the sprite: {used - owned}"

    assert 'class="bars"' in body, "the four brand colours anchor it"
    assert "runhero-stats" in body

    css = test_client.get("/static/customs.css").text
    assert "prefers-reduced-motion" in css, "the drift must be stoppable"


def test_every_dashboard_is_painted_from_the_one_palette():
    """A lane cannot be amber in Grafana and red on the tile beside it.

    The palette lives in state.py and the dashboard JSON hardcodes the same
    hexes, which is a convention rather than a mechanism -- so this is the
    mechanism. Every colour literal in every dashboard must be one of the
    four the app knows, or the two surfaces have drifted and the console is
    telling two stories about the same severity.

    It is written over every dashboard on purpose: asserting it for one
    panel proved only that one panel, which is how the drift would start.
    """
    import json, pathlib, re
    from customs import state

    palette = {c.upper() for c in
               (state.SIGNAL, state.BLOCKED, state.AT_RISK, state.CLEARED)}
    seen: dict[str, set[str]] = {}
    for path in sorted(pathlib.Path("grafana/dashboards").glob("*.json")):
        found = {m.upper() for m in
                 re.findall(r'#[0-9A-Fa-f]{6}', path.read_text())}
        stray = found - palette
        assert not stray, f"{path.name} paints with {sorted(stray)}, not the palette"
        seen[path.name] = found

    assert len(seen) == 8, f"expected 8 dashboards, found {sorted(seen)}"
    # and the palette is actually used, rather than trivially satisfied by
    # dashboards that carry no colour literal at all
    assert set().union(*seen.values()) >= {state.BLOCKED.upper(),
                                           state.AT_RISK.upper(),
                                           state.CLEARED.upper()}


def test_a_lane_chart_served_as_a_file_carries_its_own_icons(console):
    """`<use href="#d-x">` resolves against the document that owns it.

    Written into the page, it finds base.html's sprite. Served as its own
    file through <img>, it finds nothing -- and an unresolved <use> draws
    silently, so the chart was emitting six icon references into a
    document with no symbols and the label gutter simply sat empty. It
    looked like a chart with a ragged left edge rather than a bug.

    So the file has to carry what it uses, and only what it uses: the
    whole sprite is 48 symbols and a lane chart needs at most a handful.
    """
    import re
    client, store, _launched, _jobs = console
    run = _judged_run(store)

    svg = client.get(f"/runs/{run.id}/lanes.svg?full=1")
    assert svg.status_code == 200

    used = set(re.findall(r'<use href="#([^"]+)"', svg.text))
    defined = set(re.findall(r'<symbol id="([^"]+)"', svg.text))
    assert used, "each lane is labelled with its taxonomy icon"
    assert used <= defined, f"icons referenced but never defined: {used - defined}"
    assert defined == used, f"carrying symbols nothing draws: {defined - used}"


def test_the_front_door_says_what_this_is_before_what_it_does(console):
    """/ used to be the upload form, which meant the first thing a judge
    arriving from a submission link met was a file picker asking for a
    master they do not have and a list of market codes meaning nothing.

    The form is one click away at /new. This page is the argument for
    why anyone should care, and it has to carry the claim honestly: the
    numbers on it are counted from the packs, not typed in.
    """
    client, _store, _launched, _jobs = console
    from customs import packs

    body = client.get("/").text
    assert client.get("/").status_code == 200

    # the pitch
    assert "One asset." in body and "Every market." in body
    assert "Observe once, judge many" in body
    assert "Grafana is a participant" in body

    # counted, not asserted -- a stale number here is a lie to a judge
    assert f">{len(packs.load())}<" in body, "jurisdiction count is live"
    assert f">{len(packs.taxonomy())}<" in body, "dimension count is live"

    # the console tabs belong behind the door, not on it
    assert "New clearance run</a>" not in body


def test_both_doors_open_and_remember_which_one_you_chose(console):
    """Not authentication, and not pretending to be: there is no secret
    and either door opens. It records the choice, which is the hook a
    reserved budget or a read-only mode would hang off later.

    They land in different places on purpose. A judge wants the work
    already done; a visitor wants to watch it happen to their own ad.
    """
    client, _store, _launched, _jobs = console

    body = client.get("/").text
    assert 'href="/enter/judge"' in body
    assert 'href="/enter/visitor"' in body

    judge = client.get("/enter/judge", follow_redirects=False)
    assert judge.status_code == 303
    assert judge.headers["location"] == "/runs"
    assert judge.cookies["customs-role"] == "judge"

    visitor = client.get("/enter/visitor", follow_redirects=False)
    assert visitor.status_code == 303
    assert visitor.headers["location"] == "/new"
    assert visitor.cookies["customs-role"] == "visitor"

    # and a door nobody built is a 404, not a silent redirect somewhere
    assert client.get("/enter/admin", follow_redirects=False).status_code == 404


def test_the_board_shows_the_thing_it_is_judging(console):
    """The launch board discussed a commercial at length and never showed
    one. Every other screen has the picture somewhere; this is the screen
    people land on, so it was the one place you could read a verdict with
    no idea what it was a verdict about.

    It asks whether there is a still BEFORE rendering, because the poster
    route answers 404 when there is nothing and a broken image in a page
    header is a hole rather than a quiet absence.
    """
    client, store, _launched, _jobs = console
    run = _judged_run(store)

    body = client.get(f"/runs/{run.id}").text
    assert "board-poster" in body
    assert f"/runs/{run.id}/poster.jpg" in body
    # and it goes where the footage actually plays
    assert f'href="/runs/{run.id}/cutting"' in body
    # no onerror escape hatch: this one is decided on the server
    assert "onerror" not in body.split("board-poster")[1][:600]


def test_the_board_still_rotates_because_one_frame_is_a_coin_flip(console):
    """Commercials open on black, on a fade, or on a logo card, so the
    frame at one second is quite often nothing at all -- and the board
    was showing that black rectangle as its only reference to the film.

    Five stills from across the middle, and the extremes left alone
    because that is exactly where the black and the end card live.
    """
    import re
    from customs.app import board_stills

    client, store, _launched, _jobs = console
    run = _judged_run(store)
    body = client.get(f"/runs/{run.id}").text

    ats = re.findall(rf"/runs/{run.id}/poster\.jpg\?at=([0-9.]+)", body)
    if not ats:
        # no master on disk: one fallback still, and no rotation claimed
        assert "rotating" not in body.split("board-poster")[1][:200]
        return

    assert len(ats) == 5, f"expected five stills, got {ats}"
    assert "rotating" in body.split("board-poster")[1][:200]
    values = [float(a) for a in ats]
    assert values == sorted(values), "stills should walk forwards through the film"

    duration = _duration_of(store, run)
    if duration:
        assert values[0] > duration * 0.02, "the opening frames are where the black is"
        assert values[-1] < duration * 0.95, "and the end card lives at the other end"

    # every one of them is really servable, not just a URL we wrote
    for at in ats[:2]:
        shot = client.get(f"/runs/{run.id}/poster.jpg?at={at}")
        assert shot.status_code == 200, f"still at {at}s did not render"
        assert shot.headers["content-type"] == "image/jpeg"


def _duration_of(store, run):
    from customs.app import asset_duration
    try:
        return asset_duration(run)
    except Exception:
        return None


def test_a_judge_gets_the_archive_and_a_visitor_gets_a_clean_slate(console):
    """Two doors, two different rooms behind them.

    A judge came to read what this has already done, so they get every
    run. Someone who just walked in came to watch it happen to their own
    ad, and twenty of someone else's runs is not a welcome -- it is a
    wall between them and the one thing they wanted to try.

    Nothing is hidden and this is not a boundary: every run is still
    reachable by its URL. It is a reading convenience, which is exactly
    why the list of "mine" can live in a cookie rather than in the store.
    """
    client, store, _launched, _jobs = console
    _judged_run(store)
    _judged_run(store)

    # no door used at all -- a direct link, a bookmark -- sees everything
    plain = client.get("/runs")
    assert plain.text.count('class="runrow"') == 2

    # the judge's door: same
    client.get("/enter/judge")
    assert client.get("/runs").text.count('class="runrow"') == 2

    # the visitor's door: their own runs, of which there are none yet
    client.cookies.clear()
    client.get("/enter/visitor")
    fresh = client.get("/runs")
    assert fresh.text.count('class="runrow"') == 0
    assert "Nothing cleared yet" in fresh.text
    assert 'href="/new"' in fresh.text, "and a way to start one"
    assert "Your runs" in fresh.text

    # and a run they start does show up
    response = _upload(client)
    assert response.status_code == 303
    mine = client.get("/runs")
    # class="runrow" or class="runrow sparkle" -- a just-uploaded run is in
    # flight, and in-flight work wears the sparkle everywhere
    assert mine.text.count('class="runrow') == 1, "their own run, and only theirs"


def test_a_lane_is_never_shorter_than_the_glyph_that_labels_it():
    """The icon size and the row height were two independent numbers set
    at two call sites, so doubling the icon silently overlapped every
    label with the one below it. The chart stayed correct and became
    unreadable, which is the worst kind of wrong.

    They are tied together now: whatever a caller asks for, a row is at
    least its icon plus a little air.
    """
    import re
    from customs import spark

    rows = [{"dimension": d, "events": [{"t": 1.0, "flagged": False, "severity": 0}]}
            for d in ("alcohol_tobacco_drugs", "text_legibility", "gender_portrayal")]

    # a caller asking for the impossible gets the readable thing instead
    svg = spark.lanes(rows, 20.0, width=560, row_h=10, icon=38, ruler=False)
    ys = [float(y) for y in re.findall(r'<use href="#d-[a-z_0-9]+" x="4" y="([0-9.-]+)"', svg)]
    assert len(ys) == 3
    gaps = [b - a for a, b in zip(ys, ys[1:])]
    assert all(g >= 38 for g in gaps), f"labels overlap: {gaps}"

    # and the svg grew to hold them rather than clipping
    height = float(re.search(r'height="([0-9.]+)"', svg).group(1))
    assert height >= ys[-1] + 38


def test_every_method_the_picker_offers_is_a_method_the_route_accepts():
    """A method the console offers and the route rejects is a dead button.

    The route used to restate the list, and it went stale the moment a
    method was added -- per_frame was offered, priced, and then 400'd.
    """
    from customs import costs
    import inspect
    from customs import app as app_mod

    source = inspect.getsource(app_mod.remediate_now)
    assert "costs.METHODS" in source, \
        "the route should derive its allow-list, not restate it"

    for method in costs.METHODS:
        # priced and gate-able: enough to prove it is a real, offerable method
        assert costs.estimate(method.key, 4.0) >= 0
        assert isinstance(costs.available(method.key, 4.0, 0.0), tuple)


def test_a_run_card_shows_what_is_still_open_before_it_clears(client):
    """The archive said which markets objected, never how much of it is
    still standing. The gauge is that number, and it is drawn inline --
    market_states already counted open, resolved and total for every row,
    so fetching it back would be a round trip for what we were holding."""
    from customs.app import clearance_gauge
    from customs import state

    test_client, _store, _run, _ = client
    body = test_client.get("/runs").text
    assert 'class="cardgauge"' in body

    # nothing open on a clean run: an empty arc, in the cleared colour
    clean = clearance_gauge({"FR": {"clearance": "cleared", "open": 0, "findings": 0}})
    assert state.CLEARED in clean and clean.count("<path") == 1

    # something open but cleared to air: amber, part-filled
    noted = clearance_gauge({"FR": {"clearance": "cleared", "open": 2, "findings": 5}})
    assert state.AT_RISK in noted and noted.count("<path") == 2
    assert ">2<" in noted

    # a blocked market outranks it
    blocked = clearance_gauge({"FR": {"clearance": "cleared", "open": 1, "findings": 3},
                               "SA": {"clearance": "blocked", "open": 4, "findings": 4}})
    assert state.BLOCKED in blocked and ">5<" in blocked


def test_agent_mode_can_be_handed_a_file_without_leaving_the_conversation(client):
    """The point of agent mode is not having to go and find the form."""
    test_client, _store, _run, _ = client
    body = test_client.get("/agent").text
    assert 'id="agent-drop"' in body and 'id="agent-file"' in body

    js = test_client.get("/static/customs.js").text
    # it posts to the SAME route the form uses, so the caps and the
    # plain-text rejections do not have to be reimplemented
    assert 'fetch("/runs", { method: "POST"' in js
    assert '"GLOBAL"' in js and '"EU"' in js, "a run starts immediately, markets refine later"


def test_the_gauge_is_a_gauge_and_is_centred_on_its_arc():
    """Two bugs behind one complaint that it "was not centralised well".

    The arc opened to the RIGHT, not the bottom: the point helper had the
    x sign inverted, which mirrors the circle and lands both open ends on
    the same side. A gauge's gap goes at the bottom.

    And it was centred on its box rather than on the arc. A 270-degree arc
    reaches a full radius above its centre and only r*sin(45) below, so
    centring the CENTRE left three times as much air above as below.
    """
    import math, re
    from customs import spark

    svg = spark.gauge(3, 12, width=168, height=104)
    m = re.search(r'M([0-9.]+) ([0-9.]+) A([0-9.]+) [0-9.]+ 0 [01] 1 ([0-9.]+) ([0-9.]+)', svg)
    assert m, svg[:200]
    x0, y0, r, x1, y1 = (float(v) for v in m.groups())

    # the two open ends are on opposite sides, level with each other
    assert abs(x0 - x1) > r, f"the arc opens to one side: x {x0} and {x1}"
    assert abs(y0 - y1) < 0.5, "the gap is not level, so it is not at the bottom"
    assert abs((x0 + x1) / 2 - 84) < 1, "the arc is not centred horizontally"

    # apex is a full radius above the centre; the tips are the low points
    cy = y0 - r * math.sin(math.radians(45))
    above, below = cy - r, 104 - max(y0, y1)
    assert abs(above - below) < 1.5, f"{above:.1f}px above, {below:.1f}px below"


def test_the_gauge_is_centred_horizontally_in_the_card():
    """The card is a grid, so the span holding the gauge is a grid item.
    `margin: 0 auto` on the svg centred it inside a box that was not
    itself centred; the container does the centring now."""
    from pathlib import Path
    css = Path("src/customs/static/customs.css").read_text()
    block = css[css.index(".cardgauge {"):css.index(".cardgauge {") + 160]
    assert "justify-content: center" in block, block


def test_the_pickers_word_is_law(console, monkeypatch, tmp_path):
    """Three explicit "Regenerate with Veo" picks once became two centre
    crops, because everything except bridge collapsed into plan()'s choice.
    Now: per_frame passes through as itself, overlay forces the single-frame
    freeze landing, track keeps the relight propagation, and the webhook's
    "auto" stays the planner's call. Called through _remediate_and_verify
    directly because the console fixture stubs the background job."""
    client, store, _launched, _jobs = console
    run = _judged_run(store)
    fid = "fnd_FR_FR-ALC-01_obs_shot_0_000"
    got = []

    monkeypatch.setattr(app_module.remediate, "plan",
                        lambda finding, observation=None: "prop_swap")
    monkeypatch.setattr(app_module.remediate, "apply",
                        lambda run_arg, finding, chosen, workdir, db, **kw:
                        got.append((chosen, kw.get("landing"))) or object())
    monkeypatch.setattr(app_module.verify, "confirm",
                        lambda *a, **k: True)
    monkeypatch.setattr(app_module.persist, "snapshot", lambda *a, **k: "snap")
    monkeypatch.setattr(app_module, "asset_duration", lambda run_arg: 42.0)

    for method, expected in (("overlay", ("prop_swap", "freeze")),
                             ("track", ("prop_swap", None)),
                             ("per_frame", ("per_frame", None)),
                             ("omni", ("omni", None)),
                             ("auto", ("prop_swap", None))):
        store.update_finding_status(fid, "open", run_id=run.id)
        # the underscored one: the console fixture stubs the public wrapper
        assert app_module._remediate_and_verify(
            run.id, fid, "FR", tmp_path, method=method) is True, method
        assert got[-1] == expected, method


def test_boot_sweeps_statuses_no_thread_can_own(console, monkeypatch, tmp_path):
    """A deploy replaces the single container, killing any remediation
    thread mid-edit: the finding it moved to "remediating" showed a
    "Working" row in the market room forever (observed live), and a run
    killed mid-pipeline polled at "running" for good. At boot nothing can
    own either status, so the sweep puts them back honestly."""
    client, store, _launched, _jobs = console
    run = _judged_run(store)
    fid = "fnd_FR_FR-ALC-01_obs_shot_0_000"
    store.update_finding_status(fid, "remediating", run_id=run.id)
    dead = store.create_run(asset_path=ASSET, markets=["FR"])
    store.set_run_status(dead.id, "running")

    monkeypatch.setattr(app_module.persist, "state_dir", lambda: tmp_path)
    app_module._sweep_orphaned_work()

    assert {f.id: f.status for f in store.findings(run.id, "FR")}[fid] == "open"
    assert store.get_run(dead.id).status == "error"
    assert any("service restarted" in m for _i, _t, _a, m
               in store.events_since(run.id, 0))
    # without a state dir (dev, tests) the sweep must be a no-op
    store.update_finding_status(fid, "remediating", run_id=run.id)
    monkeypatch.setattr(app_module.persist, "state_dir", lambda: None)
    app_module._sweep_orphaned_work()
    assert {f.id: f.status for f in store.findings(run.id, "FR")}[fid] == "remediating"


def test_ops_busy_reports_what_a_deploy_would_destroy(console):
    """A deploy replaces the single container: an Omni edit once finished on
    the old container seconds after the new one had restored, and the paid
    master was silently discarded. deploy.sh gates on this answer."""
    client, store, _launched, _jobs = console
    run = _judged_run(store)

    body = client.get("/ops/busy").json()
    assert body["busy"] is False

    store.update_finding_status("fnd_FR_FR-ALC-01_obs_shot_0_000",
                                "remediating", run_id=run.id)
    body = client.get("/ops/busy").json()
    assert body["busy"] is True
    assert body["remediating_findings"] == ["fnd_FR_FR-ALC-01_obs_shot_0_000"]
    assert body["remediating_runs"] == [run.id], "the beacon needs a run to link to"

    store.update_finding_status("fnd_FR_FR-ALC-01_obs_shot_0_000",
                                "open", run_id=run.id)
    live = store.create_run(asset_path=ASSET, markets=["FR"])
    store.set_run_status(live.id, "running")
    body = client.get("/ops/busy").json()
    assert body["busy"] is True and live.id in body["running_runs"]


def test_the_lifecycle_strip_knows_where_a_run_stands(console):
    """Five flat tabs never told a first-timer that the mission feed IS the
    processing stage or the cutting room IS the result. The lifecycle helper
    lights the current stage and hands the one state-driven next step."""
    client, store, _launched, _jobs = console

    live = store.create_run(asset_path=ASSET, markets=["FR"])
    store.set_run_status(live.id, "running")
    lc = app_module.run_lifecycle(store.get_run(live.id))
    assert {s["key"]: s["state"] for s in lc["stages"]}["processing"] == "current"
    assert "mission" in lc["cta"]["href"]

    judged = _judged_run(store)
    lc = app_module.run_lifecycle(store.get_run(judged.id))
    states = {s["key"]: s["state"] for s in lc["stages"]}
    assert states["processing"] == "done" and states["decision"] == "current"
    assert "/markets/" in lc["cta"]["href"], "the next step names a failing market"

    for f in store.findings(judged.id):
        store.update_finding_status(f.id, "resolved", run_id=judged.id)
    lc = app_module.run_lifecycle(store.get_run(judged.id))
    assert {s["key"]: s["state"] for s in lc["stages"]}["verified"] == "done"
    assert lc["cta"]["href"].endswith("/cutting")

    # and the strip plus breadcrumb render on a run screen
    page = client.get(f"/runs/{judged.id}").text
    assert "All runs" in page and 'class="wrap lifecycle"' in page


def test_the_market_room_clusters_findings_by_scene(console):
    """Same device as the frame board: a shot with several objections is one
    section under one header, and the scenescroll on top jumps straight to
    a scene's rows."""
    client, store, _launched, _jobs = console
    run = _judged_run(store)
    # a second scene: the fixture's FR findings both hang off shot_0's
    # observation, and one scene alone earns no scroll
    store.add_findings([_finding(
        run.id, id="fnd_FR_FR-MOD-01_obs_shot_1_000", rule_id="FR-MOD-01",
        observation_id="obs_shot_1_000", t_start=4.2, t_end=5.1,
        rationale="a second scene's objection")])

    page = client.get(f"/runs/{run.id}/markets/FR").text
    assert 'id="mk-shot_0"' in page and 'id="mk-shot_1"' in page
    assert 'href="#mk-shot_0"' in page, "the scenescroll jumps to the scene"
    assert page.count('class="scene-row"') == 2


def test_the_landing_page_plays_a_before_and_after(console):
    """The front door shows a fix landing: the wine toast and the same six
    seconds re-rendered compliant, side by side, in lockstep. The clips are
    the project's own footage, edited by the product's own method."""
    client, _store, _launched, _jobs = console
    page = client.get("/").text
    for key in ("wine", "smoke", "skirt"):
        assert f"fix-{key}-before.mp4" in page and f"fix-{key}-after.mp4" in page
    assert page.count("data-lockstep") == 6 and "Loi" in page
    from pathlib import Path
    static = Path("src/customs/static")
    for key in ("wine", "smoke", "skirt"):
        assert (static / f"fix-{key}-before.mp4").stat().st_size > 100_000
        assert (static / f"fix-{key}-after.mp4").stat().st_size > 100_000


def test_a_viz_click_launches_a_remediation_by_coordinate(console):
    """The Grafana test: a data link can only navigate, and what it carries
    is the click's coordinate -- the dimension series and the mapped-clock
    timestamp. That pair plus the run resolves to the open finding under
    the click, and the workflow starts."""
    client, store, _launched, jobs = console
    run = _judged_run(store)

    # by coordinate, film seconds
    r = client.get(f"/launch/remediate?run={run.id}"
                   f"&dimension=alcohol_tobacco_drugs&t=1.5&method=omni",
                   follow_redirects=False)
    assert r.status_code == 303 and f"/runs/{run.id}/markets/FR" in r.headers["location"]
    assert "#mk-shot_0" in r.headers["location"]
    assert len(jobs) == 1 and jobs[0][1] == "fnd_FR_FR-ALC-01_obs_shot_0_000"

    # by coordinate, Grafana's epoch milliseconds on the mapped clock
    ms = (store.get_run(run.id).t0 + 1.5) * 1000
    r = client.get(f"/launch/remediate?run={run.id}"
                   f"&dimension=alcohol_tobacco_drugs&t={ms}&method=omni",
                   follow_redirects=False)
    assert r.status_code == 303 and len(jobs) == 2

    # by finding id, the console matrix's direct route
    r = client.get(f"/launch/remediate?run={run.id}"
                   f"&finding=fnd_FR_FR-ALC-01_obs_shot_0_000&method=omni",
                   follow_redirects=False)
    assert r.status_code == 303 and len(jobs) == 3

    # nothing at that coordinate -> honest 404, nothing launched
    r = client.get(f"/launch/remediate?run={run.id}"
                   f"&dimension=children_and_minors&t=1.5",
                   follow_redirects=False)
    assert r.status_code == 404 and len(jobs) == 3


def test_the_timeline_draws_the_matrix_with_launch_cells(console):
    client, store, _launched, _jobs = console
    run = _judged_run(store)
    page = client.get(f"/runs/{run.id}/timeline").text
    assert 'class="mg"' in page, "occurrence types by scenes"
    assert "alcohol tobacco drugs" in page
    # with no viewer the console draws the body itself, cells and all
    assert f"/launch/remediate?run={run.id}&finding=" in page, \
        "a hot cell is a launch button"
    assert "--cols:" in page and "fr" in page, "columns are weighted by scene length"


def test_the_board_frames_live_grafana_when_the_viewer_is_deployed(console, monkeypatch):
    """Grafana Cloud will not be framed -- frame-ancestors 'none' on every
    dashboard URL, 403 on the settings API -- so the board renders its panels
    as PNGs. When the embeddable viewer is deployed (scripts/deploy_viewer.sh)
    the same dashboards arrive live instead, from a Grafana whose datasources
    proxy through this very stack."""
    client, store, _launched, _jobs = console
    run = _judged_run(store)

    page = client.get(f"/runs/{run.id}").text
    assert "/lanes.png" in page and "<iframe" not in page, "PNG until a viewer exists"

    # settings is a frozen dataclass, so swap in a copy carrying the viewer
    import dataclasses
    monkeypatch.setattr(app_module, "settings", dataclasses.replace(
        app_module.settings, grafana_viewer_url="https://viewer.example.run.app"))
    page = client.get(f"/runs/{run.id}").text
    assert "https://viewer.example.run.app/d/customs-lanes/customs" in page
    assert "kiosk" in page and "var-run=" in page, "framed without chrome, on this run"
    assert "<iframe" in page and "/lanes.png" not in page


def test_the_timeline_pairs_its_matrix_with_the_live_grafana(console, monkeypatch):
    """The matrix is the console's -- no Grafana panel puts screenshots on an
    axis -- so the page shows both: our grid, and the same coordinates drawn
    by Grafana itself, where a click launches the same workflow."""
    import dataclasses
    client, store, _launched, _jobs = console
    run = _judged_run(store)

    page = client.get(f"/runs/{run.id}/timeline").text
    assert 'class="mg"' in page and "<iframe" not in page

    monkeypatch.setattr(app_module, "settings", dataclasses.replace(
        app_module.settings, grafana_viewer_url="https://viewer.example.run.app"))
    page = client.get(f"/runs/{run.id}/timeline").text
    assert 'class="mg"' in page, "the console keeps drawing the axes"
    # ... and Grafana's squares become the body between them
    assert "https://viewer.example.run.app/d-solo/customs-grid/the-grid" in page
    assert "panelId=1" in page and 'class="mg-live"' in page
    assert 'class="mg-cells"' not in page, "the console body steps aside"


def test_an_evidence_frame_can_be_asked_for_small(console, monkeypatch, tmp_path):
    """The kept frames are full-resolution PNGs over a megabyte each, and the
    scene grid draws eighteen of them as a 42px strip -- twenty megabytes to
    paint a row of thumbnails. A width asks for the size actually shown, and
    widths are clamped to a short list so the cache cannot be filled by
    asking for every integer."""
    client, store, _launched, _jobs = console
    run = _judged_run(store)
    frame = tmp_path / "kf.png"
    frame.write_bytes(b"\x89PNG full size")
    store.add_observations(run.id, [Observation(
        id="obs_thumb", shot_id="shot_9", t_start=1.0, t_end=2.0,
        dimension="text_legibility", statement="text", evidence_frame=str(frame),
        confidence=0.5)])

    asked = []
    monkeypatch.setattr(app_module.media, "thumbnail",
                        lambda src, width, out: asked.append(width) or frame)

    assert client.get(f"/runs/{run.id}/evidence/obs_thumb").status_code == 200
    assert asked == [], "no width asked, no thumbnail made"

    client.get(f"/runs/{run.id}/evidence/obs_thumb?w=160")
    client.get(f"/runs/{run.id}/evidence/obs_thumb?w=99")
    client.get(f"/runs/{run.id}/evidence/obs_thumb?w=9999")
    assert asked == [160, 160, 640], "clamped to the allowed sizes"

    # a thumbnail that cannot be made still serves the real frame
    monkeypatch.setattr(app_module.media, "thumbnail",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no ffmpeg")))
    assert client.get(f"/runs/{run.id}/evidence/obs_thumb?w=160").status_code == 200


def test_a_market_scene_shows_how_it_opens_and_how_it_closes(console, tmp_path):
    """The market room clustered its findings by scene but never showed the
    scene: one thumbnail at best, and only if a finding happened to carry
    it. Now each scene header opens with the shot's own first and last kept
    frame, side by side, whether or not a finding sits on them."""
    client, store, _launched, _jobs = console
    run = _judged_run(store)

    # two kept frames in the same shot: the scene opens on one, closes on
    # the other, and neither carries a finding of its own
    head, tail = tmp_path / "a.png", tmp_path / "b.png"
    for f in (head, tail):
        f.write_bytes(b"\x89PNG frame")
    store.add_observations(run.id, [
        Observation(id="obs_shot_0_010", shot_id="shot_0", t_start=0.2, t_end=1.0,
                    dimension="text_legibility", statement="opens",
                    evidence_frame=str(head), confidence=0.5),
        Observation(id="obs_shot_0_011", shot_id="shot_0", t_start=6.0, t_end=7.0,
                    dimension="text_legibility", statement="closes",
                    evidence_frame=str(tail), confidence=0.5)])

    page = client.get(f"/runs/{run.id}/markets/FR").text
    assert 'class="sc-pair"' in page, "the scene shows itself"
    assert f"/runs/{run.id}/evidence/obs_shot_0_010?w=320" in page, "how it opens"
    assert f"/runs/{run.id}/evidence/obs_shot_0_011?w=320" in page, "how it closes"


def test_the_archive_carries_one_live_panel_not_thirty_five(console, monkeypatch):
    """Every card draws its own charts as SVG because building them inline
    once took this page past a two minute timeout -- and an iframe per card
    would be thirty-five Grafana applications in one browser. So the archive
    gets a single instance-wide panel, and a visitor scoped to their own runs
    gets none, because it is not their view to see."""
    import dataclasses
    client, store, _launched, _jobs = console
    _judged_run(store)

    assert "<iframe" not in client.get("/runs").text, "no viewer, no panel"

    monkeypatch.setattr(app_module, "settings", dataclasses.replace(
        app_module.settings, grafana_viewer_url="https://viewer.example.run.app"))
    page = client.get("/runs").text
    assert "d-solo/customs-history/customs?panelId=1" in page, "the one at the top"
    # the cards frame their own lanes too, but lazily -- only what is near
    # the viewport ever boots a Grafana
    assert page.count('class="cardlanes live"') >= 1
    assert page.count('loading="lazy"') >= page.count("<iframe") - 1

    client.get("/enter/visitor")
    assert "<iframe" not in client.get("/runs").text, "not on a scoped archive"


def test_a_click_on_the_live_panel_opens_that_assets_newest_run(console):
    """The panel's series are labelled by asset, because that is what the crew
    writes to Loki, and an asset can have been cleared more than once."""
    client, store, _launched, _jobs = console
    old = _judged_run(store)
    new = _judged_run(store)   # same asset, cleared again

    r = client.get(f"/runs/by-asset?asset={Path(ASSET).stem}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/runs/{new.id}"
    assert old.id != new.id, "newest wins"

    r = client.get("/runs/by-asset?asset=never-cleared", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/runs"


def test_a_run_card_plays_itself_on_hover(console, tmp_path, monkeypatch):
    """The card's still is the poster of a video that is not fetched until a
    pointer lands on it -- preload="none" is what makes thirty-five of them
    free. The clip itself is the whole film in a few seconds, built once."""
    client, store, _launched, _jobs = console
    run = _judged_run(store)

    page = client.get("/runs").text
    assert 'class="runthumb" data-hoverplay' in page
    # metadata, not none: a clip with nothing buffered took too long to
    # start when a lazy Grafana was booting beside every card
    assert 'preload="metadata"' in page and "/preview.mp4" in page
    assert f'poster="/runs/{run.id}/poster.jpg"' in page, "it paints as it did"

    made = []
    clip = tmp_path / "preview.mp4"
    clip.write_bytes(b"mp4")
    monkeypatch.setattr(app_module.media, "preview_clip",
                        lambda src, out, **kw: made.append(str(out)) or clip)
    assert client.get(f"/runs/{run.id}/preview.mp4").status_code == 200
    assert made, "built on first ask"

    # a run whose master is gone keeps its poster instead of 500ing
    monkeypatch.setattr(app_module.media, "preview_clip",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no ffmpeg")))
    assert client.get(f"/runs/{run.id}/preview.mp4").status_code == 404


def test_a_run_can_be_removed_but_not_the_load_bearing_ones(console, monkeypatch):
    """Deleting a run erases its rows and its artifacts. What it must never
    erase is the day's spend ledger -- euros that were spent stay spent, or
    deleting a run becomes a way to buy another Veo generation."""
    client, store, _launched, _jobs = console
    run = _judged_run(store)
    store.record_spend("bridge", 3.68, run.id, "fnd_x")
    spent_before = store.spent_today()

    assert client.post(f"/runs/{run.id}/delete",
                       follow_redirects=False).status_code == 303
    assert store.get_run(run.id) is None
    assert store.findings(run.id) == [] and store.observations(run.id) == []
    assert store.spent_today() == spent_before, "the ledger is not a refund"

    # the pinned showcase is refused: the archive and the front door link to it
    keep = _judged_run(store)
    monkeypatch.setattr(app_module, "SHOWCASE_RUN", keep.id)
    r = client.post(f"/runs/{keep.id}/delete", follow_redirects=False)
    assert r.status_code == 409 and "pins" in r.json()["detail"]
    assert store.get_run(keep.id) is not None

    # and so is a run with work in flight
    busy = _judged_run(store)
    store.set_run_status(busy.id, "running")
    r = client.post(f"/runs/{busy.id}/delete", follow_redirects=False)
    assert r.status_code == 409 and "still running" in r.json()["detail"]

    # a visitor cannot delete a run that is not theirs, and is not told it exists
    other = _judged_run(store)
    client.get("/enter/visitor")
    assert client.post(f"/runs/{other.id}/delete",
                       follow_redirects=False).status_code == 404
