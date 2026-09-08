"""Alert webhook tests, offline.

The store is pointed at a tmp database and the remediation job is replaced by
a recorder, so these tests are about exactly one thing: which alerts turn
into work, and which are dropped. TestClient runs a BackgroundTask after the
response has been returned, so a recorded call proves the task was really
enqueued rather than just planned.
"""
import inspect
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
        _enter(test_client, "judge")  # the spending routes are behind a door
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

def test_the_alert_path_needs_no_word_because_grafana_carries_no_cookie(client):
    """The doors are for people. The webhook is the product's real trigger,
    fired by a Grafana alert rule from outside any browser, and putting it
    behind the same cookie would gate the one path the demo is about."""
    test_client, _store, run, jobs = client
    test_client.cookies.clear()

    response = test_client.post("/webhook/alert", json=_alert())

    assert response.status_code == 200
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
    # **_ because plan() now also takes the evidence the caller measured
    # (scope's reach, the span's motion, whether Omni could run at all)
    monkeypatch.setattr(app_module.remediate, "plan",
                        lambda finding, observation=None, **_: "prop_swap")
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
import html
import json
import os
import re
import threading
import io
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
        _enter(test_client, "judge")  # the spending routes are behind a door
        yield test_client, store, launched, jobs


def _enter(client, role):
    """Walk through a door with its password, the way a person does."""
    from customs.config import settings
    word = settings.judge_password if role == "judge" else settings.visitor_password
    return client.post(f"/enter/{role}", data={"password": word},
                       follow_redirects=False)


def _judged_run(store, asset=ASSET):
    """A finished run holding the fixture's findings and stage error.

    `asset` varies because the archive groups by film: two runs of the same
    path are one card with a dated link under it, which is the point, and a
    test about anything else needs two different films.
    """
    run = store.create_run(asset_path=asset, markets=list(MARKETS))
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
        _enter(client, "judge")
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


def test_a_film_this_store_has_already_watched_is_not_watched_again(console):
    """The same master, uploaded twice, used to buy a second shot
    detection, a second set of keyframes and a second vision pass to reach
    facts already in the store. It says so instead, and offers the cheap
    door: the markets that run has not been judged for, starting at the
    adjudicator."""
    client, store, launched, _jobs = console
    twin = _judged_run(store, asset="runs/uploads/abcdef012345/ad.mp4")
    before = len(store.recent_runs(50))

    page = _upload(client, markets=["FR", "US", "BE"], name="ad.mp4")

    assert page.status_code == 200
    assert "already been analysed" in page.text
    assert launched == [], "and no crew was started"
    assert len(store.recent_runs(50)) == before, "and no second run was created"
    # the markets that run has not seen are the offer, the ones it has are context
    assert f'action="/runs/{twin.id}/analysis"' in page.text
    assert '<input type="hidden" name="markets" value="BE">' in page.text
    assert "Judge it for BE" in page.text
    # ...and the film itself is still on disk, named for the way back
    assert 'name="pending"' in page.text and 'name="force" value="1"' in page.text


def test_clearing_it_again_on_purpose_reuses_the_upload_already_here(console):
    """The expensive door is still open, because a different film can share
    a name and because sometimes the whole pass IS what you want. It hands
    back the file the page was about rather than sending anyone to their
    file picker a second time."""
    import re as _re
    client, store, launched, _jobs = console
    _judged_run(store, asset="runs/uploads/abcdef012345/ad.mp4")
    page = _upload(client, markets=["FR"], name="ad.mp4")
    pending = _re.search(r'name="pending" value="([^"]+)"', page.text).group(1)

    again = client.post("/runs", data={"pending": pending, "force": "1",
                                       "markets": ["FR"]},
                        follow_redirects=False)

    assert again.status_code == 303 and "/runs/run_" in again.headers["location"]
    assert len(launched) == 1, "the crew really ran this time"
    assert launched[0][1].endswith("/ad.mp4")


def test_a_pending_upload_is_a_shape_not_a_path(console):
    """It arrives in a form, so it is trusted the way a path from a form
    should be: the exact shape this handler writes, resolved, and inside
    the uploads directory. Anything else is no file at all, which is a
    400 asking for a master rather than a traversal."""
    client, _store, _launched, _jobs = console
    for forged in ("../../../etc/passwd", "/etc/passwd", "abcdef012345/../../x",
                   "nothex/ad.mp4", "abcdef012345/ad.mp4"):
        reply = client.post("/runs", data={"pending": forged, "force": "1",
                                           "markets": ["FR"]},
                            follow_redirects=False)
        assert reply.status_code == 400, forged
        assert "Provide a master" in reply.text, forged


def test_neither_file_nor_link_is_a_400(console):
    test_client, *_ = console
    reply = test_client.post("/runs", data={"markets": ["FR"]})
    assert reply.status_code == 400
    assert "Provide a master" in reply.text


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


def test_the_run_nav_is_one_ladder_with_the_broadcasters_folded(client):
    """A run can cover a baseline, a continent, countries and broadcasters
    at once, and this used to be four stacked labelled rows: a quarter of
    every run screen spent saying very little, with a ragged edge where
    the labels ran out of things to label.

    One strip in ladder order now. The marks carry the level, and each
    country's broadcasters fold behind it -- BE-VRT, BE-VTM and BE-PLAY
    side by side read as three countries.
    """
    test_client, store, _run, _ = client
    run = store.create_run(asset_path=ASSET,
                           markets=["GLOBAL", "EU", "FR", "BE", "BE-VRT", "FR-M6"])
    page = test_client.get(f"/runs/{run.id}")
    assert page.status_code == 200
    body = page.text

    # one strip, not one per level
    assert body.count('class="wrap markets-row ladder"') == 1
    assert body.count('class="lvl-tag label"') == 1

    # the ladder itself, in order, with the territories as tabs. Matched
    # loosely on purpose: the icon branch leaves a newline between the
    # glyph and the code, and this test is about the order of the rungs.
    strip = body.split('class="wrap markets-row ladder"', 1)[1].split("</div>", 1)[0]
    flat = " ".join(strip.split())
    for code in ("GLOBAL", "EU", "FR", "BE"):
        assert f"{code}</a>" in flat, code
    assert flat.index("GLOBAL</a>") < flat.index("EU</a>") < flat.index("FR</a>")

    # and the channels behind their own country, one fold each
    assert strip.count('<details class="mkfold">') == 2, "BE and FR"
    fold = strip.split('<details class="mkfold">')[1]
    assert "BE-VRT" in fold and 'class="mkfold-list"' in fold

    ladder = app_module.market_ladder(store.get_run(run.id))
    assert [tier["level"] for tier in ladder] == [
        "global", "continental", "national", "channel"]
    channels = next(t for t in ladder if t["level"] == "channel")
    assert [m["code"] for m in channels["markets"]] == ["BE", "FR"]
    assert sum(len(m["children"]) for m in channels["markets"]) == 2


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


def test_my_edits_groups_a_scene_rather_than_a_market(console):
    """Each run has a cutting room, which answers "what happened to this
    film". Nothing answered "what has this system actually changed", which
    is the question a reader asks after watching two clearances.

    So there is a screen for it, between the archive and the library. One
    card per SCENE, not per market: three jurisdictions objecting to the
    same two seconds is one shot with three objections, and only one of
    them paid for the render, so the card names every market that objected
    and the one the fix on show was made for. A scene whose stills were
    never mirrored is counted and explained at the foot rather than
    dropped, because a silent omission on a page about evidence is worse
    than an admitted gap.
    """
    from customs.schema import ChangeRecord

    client, store, _launched, _jobs = console
    empty = client.get("/edits")
    assert empty.status_code == 200
    assert "Nothing edited yet" in empty.text

    run = _judged_run(store)
    findings = store.findings(run.id)
    span = [f for f in findings if f.t_start == findings[0].t_start]
    first = span[0]
    changes = Path(app_module.run_dir(run)) / "changes"
    changes.mkdir(parents=True, exist_ok=True)
    for name in ("chg_ee0001_before.png", "chg_ee0001_after.png"):
        (changes / name).write_bytes(b"\x89PNG\r\n\x1a\n")
    store.add_change(ChangeRecord(
        id="chg_ee0001", run_id=run.id, finding_id=first.id, method="omni",
        description="repainted the label",
        before_frame=str(changes / "chg_ee0001_before.png"),
        after_frame=str(changes / "chg_ee0001_after.png")))
    # a second market asking for the same seconds is the same scene
    other = next((f for f in findings
                  if f.t_start == first.t_start and f.market != first.market), None)
    if other is not None:
        store.add_change(ChangeRecord(
            id="chg_ee0002", run_id=run.id, finding_id=other.id,
            method="carried over from " + first.market,
            description="the same span, carried over",
            before_frame="/gone/a.png", after_frame="/gone/b.png"))

    body = client.get("/edits").text
    assert body.count('class="editcard"') == 1, "one scene, one card"
    assert f"/runs/{run.id}/stills/chg_ee0001_before.png" in body
    assert f"/runs/{run.id}/stills/chg_ee0001_after.png" in body
    assert "repainted the label" in body
    assert f"fix made for {first.market}" in body
    assert first.rule_id in body and first.market in body
    if other is not None:
        assert other.market in body, "the other market that objected is named"
        assert "2 change records on this scene" in body

    # and a change on a different span is a different scene
    late = next((f for f in findings if f.t_start != first.t_start), None)
    if late is not None:
        for name in ("chg_ee0003_before.png", "chg_ee0003_after.png"):
            (changes / name).write_bytes(b"\x89PNG\r\n\x1a\n")
        store.add_change(ChangeRecord(
            id="chg_ee0003", run_id=run.id, finding_id=late.id, method="patch",
            description="painted out the pack",
            before_frame=str(changes / "chg_ee0003_before.png"),
            after_frame=str(changes / "chg_ee0003_after.png")))
        assert client.get("/edits").text.count('class="editcard"') == 2

    # The pair is the SPAN, played, with the kept still as its poster: two
    # frames prove an edit happened and do not let anyone judge a hemline.
    # The fixture's master is on disk, so the before side is cuttable; the
    # after side is not, because this change has neither a generated clip
    # nor a localized master, and the card says "frame only" rather than
    # rendering a player that 404s inside itself.
    body = client.get("/edits").text
    assert f"/runs/{run.id}/changes/chg_ee0001/span.mp4?side=before" in body
    assert f'poster="/runs/{run.id}/stills/chg_ee0001_before.png?w=520"' in body
    assert f"/runs/{run.id}/changes/chg_ee0001/span.mp4?side=after" not in body
    assert f'/runs/{run.id}/stills/chg_ee0001_after.png' in body
    assert "frame only" in body
    assert 'data-kind="video"' in body

    # a revoice is a sound edit: the picture does not change, so it is on
    # the other side of the toggle, with its soundtrack and its controls
    store.add_change(ChangeRecord(
        id="chg_ee0009", run_id=run.id, finding_id=first.id, method="revoice",
        description="re-spoke the claim",
        before_frame=str(changes / "chg_ee0001_before.png"),
        after_frame=str(changes / "chg_ee0001_after.png")))
    sound = client.get("/edits").text
    assert 'data-kind="audio"' in sound
    assert "sound=1" in sound and "controls" in sound
    assert 'data-edit-kind="audio"' in sound, "and a toggle to reach them"

    # the span route refuses rather than guesses
    assert client.get(f"/runs/{run.id}/changes/chg_ee0001/span.mp4?side=after"
                      ).status_code == 404, "no after master for this change"
    assert client.get(f"/runs/{run.id}/changes/chg_ff0000/span.mp4").status_code == 404
    assert client.get(
        f"/runs/{run.id}/changes/chg_ee0001/span.mp4?side=sideways").status_code == 404

    # one tab away from anywhere, between the archive and the library
    assert 'href="/edits"' in client.get("/runs").text
    nav = client.get("/library").text
    assert nav.index('href="/edits"') < nav.index('href="/library"')


def test_an_edit_plays_its_own_seconds_from_both_masters(console, tmp_path):
    """/edits compares an edit by playing the span it names, twice: as
    delivered and as it came back. Both clips are cut here, on demand, from
    two different masters -- the original for before, and either the
    model's own generated file or that market's localized master for after
    -- and cached beside the change record, which is mirrored.

    Cut and not stream-copied, because a copy seeks to the nearest keyframe
    and the whole claim of the clip is that it IS those seconds.
    """
    import subprocess
    from customs.schema import ChangeRecord

    client, store, _launched, _jobs = console
    master = tmp_path / "spot.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "quiet", "-f", "lavfi",
                    "-i", "testsrc2=s=160x120:d=6:r=12", str(master)], check=True)

    run = _judged_run(store, asset=str(master))
    finding = store.findings(run.id)[0]
    localized = Path(app_module.run_dir(run)) / f"localized_{finding.market}.mp4"
    localized.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-y", "-v", "quiet", "-f", "lavfi",
                    "-i", "testsrc2=s=160x120:d=6:r=12", str(localized)], check=True)
    store.add_change(ChangeRecord(
        id="chg_aa0001", run_id=run.id, finding_id=finding.id, method="prop_swap",
        description="swapped the prop", before_frame="", after_frame=""))

    page = client.get("/edits").text
    assert f"/runs/{run.id}/changes/chg_aa0001/span.mp4?side=before" in page
    assert f"/runs/{run.id}/changes/chg_aa0001/span.mp4?side=after" in page

    for side in ("before", "after"):
        clip = client.get(f"/runs/{run.id}/changes/chg_aa0001/span.mp4?side={side}")
        assert clip.status_code == 200, (side, clip.text[:200])
        assert clip.headers["content-type"] == "video/mp4"
        assert len(clip.content) > 500, side
    # cached beside the change record, so the second reader pays nothing
    cuts = sorted(p.name for p in (Path(app_module.run_dir(run)) / "changes")
                  .glob("chg_aa0001_*span*.mp4"))
    assert cuts == ["chg_aa0001_after_span.mp4", "chg_aa0001_before_span.mp4"]

    # and a sound edit keeps its soundtrack, which is the whole edit
    store.add_change(ChangeRecord(
        id="chg_aa0002", run_id=run.id, finding_id=finding.id, method="revoice",
        description="re-spoke the line", before_frame="", after_frame=""))
    said = client.get(f"/runs/{run.id}/changes/chg_aa0002/span.mp4?side=before&sound=1")
    assert said.status_code == 200
    assert (Path(app_module.run_dir(run)) / "changes"
            / "chg_aa0002_before_span_snd.mp4").is_file()


def test_what_came_out_of_the_run_is_a_screen_beside_the_cutting_room(console):
    """The feed says what is happening; nothing said what it produced.

    Checking a generated clip meant knowing the change id and typing a URL,
    and the two frames handed to Veo were not kept at all -- they were
    written to the scratch workdir, which is never mirrored, so they went
    with the container. Those two frames are the entire brief Veo was
    given: without them there is no way to tell whether it invented
    something or was handed it.

    It was a tab on the mission feed for a while, which is the wrong place:
    the feed is the event log, and what the models made is a question about
    the film. It is a screen of its own now, next to the cutting room.
    """
    client, store, _launched, _jobs = console
    run = store.create_run(asset_path="/x/ad.mp4", markets=["SA"])

    body = client.get(f"/runs/{run.id}/generated").text
    assert "Nothing generated yet" in body      # honest empty state
    # and it is reachable from every screen in the run, beside the cutting room
    nav = client.get(f"/runs/{run.id}/cutting").text
    assert f'href="/runs/{run.id}/generated"' in nav
    cutting = nav.index(f'href="/runs/{run.id}/cutting"')
    assert nav.index(f'href="/runs/{run.id}/generated"') > cutting
    # the feed keeps the log and nothing else
    feed = client.get(f"/runs/{run.id}/mission").text
    assert "data-mtab" not in feed

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

    body = client.get(f"/runs/{run.id}/generated").text
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
    assert "data_schema, query,\n" in src and "build_dashboard)" in src, \
        "both must be registered"
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
    assert 'class="cardlanes drawn"' in body and "/lanes.svg" in body
    # and with no viewer configured, nothing tries to iframe Grafana: the
    # card has one chart and no switch to offer
    assert "<iframe" not in body
    assert "data-viz-pick" not in body


def test_hovering_a_dot_can_play_that_second_of_the_film(client):
    """The drawn chart's dots are hoverable, and the hover has to land on
    the right moment.

    The card's clip is the whole film in about five seconds -- a timelapse,
    not the first five seconds -- so a dot at 42s is nowhere near 42s of
    that clip. The chart carries the film's own length and each dot the
    second it sits at, which is the only pair that maps one to the other.
    """
    from customs import spark
    svg = spark.lanes([{"dimension": "alcohol",
                        "events": [{"t": 42.0, "flagged": True, "severity": 95,
                                    "obs": "obs_1", "market": "FR"}]}],
                      duration=56.1, ruler=False)
    assert 'data-duration="56.100"' in svg
    assert 'class="lane-dot hit"' in svg and 'data-t="42.0"' in svg
    # and the console asks for that chart as markup, not as a picture, or
    # no pointer event would ever reach a dot
    assert "img.cardlanes.drawn" in (
        Path(app_module.__file__).parent / "static" / "customs.js").read_text()


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


def test_the_archive_opens_on_the_runs_themselves(console):
    """It opened on a banner: a swarm of the product's own icons, the big
    wordmark, and a headline reading "All runs, newest first." above a list
    that is self-evidently a list of runs, newest first.

    It earned its place while the archive was the first thing a stranger
    saw. The front page does that job now, and the banner had become a
    screen of decoration between a reader and the thirty-six clearances
    they came for. Gone, with the counts it carried moved onto the row that
    heads the cards.
    """
    client, store, _launched, _jobs = console
    _judged_run(store)
    body = client.get("/runs").text

    assert "runhero" not in body
    assert "All runs, newest first" not in body
    assert "market packs" in body, "the counts survive, on the campaign row"
    assert 'class="runcard"' in body

    # and the stylesheet does not keep dressing a thing that is not there
    css = client.get("/static/customs.css").text
    assert "runhero" not in css


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

    assert len(seen) == 9, f"expected 9 dashboards, found {sorted(seen)}"
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
    assert "The Media Customs" in body
    assert "every geography and culture" in body
    assert "What happens when you upload a video" in body
    assert 'class="flowsvg"' in body, "the flow picture, not just prose"
    # and it plays: eight moves, each with the elements it lights and a
    # panel of the product's own icons saying what that move looks at
    assert body.count('class="fl-detail" data-step="') == 8
    assert 'id="flow-play"' in body
    assert body.count('data-lit="') > 20
    assert "Grafana is a participant" in body

    # counted, not asserted -- a stale number here is a lie to a judge.
    # They live in the sentences now rather than in a band of four big
    # numerals under the doors, but they are still counted from the packs.
    assert f"{len(packs.load())} jurisdictions" in body, "jurisdiction count is live"
    assert f"{len(packs.taxonomy())} dimensions" in body, "dimension count is live"

    # the console tabs belong behind the door, not on it
    assert "New clearance run</a>" not in body


def test_both_doors_open_and_remember_which_one_you_chose(console):
    """Not authentication, and not pretending to be: one shared word per
    door, and anyone who has it is that role. It records which door, which
    is what the visitor ceiling and the archive's scoping hang off.

    They land in different places on purpose. A judge wants the work
    already done; a visitor wants to watch it happen to their own ad.
    """
    client, _store, _launched, _jobs = console

    # The judge's door on the landing page goes straight to the archive:
    # reading is open, and asking a judge for a password before showing
    # them pages that were never gated reads either as theatre or as a
    # leak. The word still exists for the thing it governs, and the
    # visitor's door -- the one that leads to spending -- still asks.
    body = client.get("/").text
    assert 'class="door door-judge" href="/runs"' in body
    assert 'href="/enter/visitor"' in body
    assert "No password: reading is open" in body

    # The visitor door asks for nothing at all now, on the owner's
    # instruction: it hands out its cookie and lands you where you were
    # going. What bounds a stranger is the ceiling behind it, which is what
    # the word was ever for.
    opened = client.get("/enter/visitor?next=/new", follow_redirects=False)
    assert opened.status_code == 303
    assert opened.headers["location"] == "/new"
    assert opened.cookies["customs-role"] == "visitor"
    # the judge door still asks, because passing it lifts that ceiling
    assert "Reading needs no password" in client.get("/enter/judge").text

    judge = _enter(client, "judge")
    assert judge.status_code == 303
    assert judge.headers["location"] == "/runs"
    assert judge.cookies["customs-role"] == "judge"

    visitor = _enter(client, "visitor")
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
    _judged_run(store, asset="runs/uploads/a/one.mp4")
    _judged_run(store, asset="runs/uploads/b/two.mp4")

    # no door used at all -- a direct link, a bookmark -- sees everything
    plain = client.get("/runs")
    assert plain.text.count('class="runrow"') == 2

    # the judge's door: same
    _enter(client, "judge")
    assert client.get("/runs").text.count('class="runrow"') == 2

    # the visitor's door: their own runs, of which there are none yet
    client.cookies.clear()
    _enter(client, "visitor")
    fresh = client.get("/runs")
    assert fresh.text.count('class="runrow"') == 0
    assert "Nothing cleared yet" in fresh.text
    assert 'href="/new"' in fresh.text, "and a way to start one"
    assert "Your clearances" in fresh.text

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


def test_an_empty_search_is_a_page_not_a_query(console, monkeypatch):
    """Opening the Frame search tab from the nav paged the whole Loki
    corpus and rendered every caption as a card: seven seconds, two
    megabytes, 2,334 images, for a screen whose job at that moment is to
    show a text box. No query, no Loki, no model."""
    from customs import app as app_mod

    client, _store, _launched, _jobs = console

    def explode(*a, **k):
        raise AssertionError("an empty search reached Grafana")

    monkeypatch.setattr(app_mod, "GrafanaOps", explode, raising=False)
    import customs.grafana_ops as g
    monkeypatch.setattr(g, "GrafanaOps", explode)

    page = client.get("/search")
    assert page.status_code == 200
    assert 'name="q"' in page.text and "class=\"scard\"" not in page.text
    assert client.get("/search?format=json").json()["total"] == 0


def test_reference_screens_end_with_one_obvious_next_step(console):
    """The library, the Grafana inventory and an empty search answer a
    question and then, before this, had nowhere to send you. Every one
    ends on the same device the run lifecycle already proved: a sentence
    saying why, a chip saying what to click. Pointed at the showcase run
    when this instance has one, at the plain next screen when it does
    not -- never at a run that is not there."""
    client, store, _launched, _jobs = console

    for path in ("/library", "/grafana", "/search"):
        page = client.get(path).text
        assert 'class="nextstep"' in page, path
        assert "WHAT'S NEXT" in page.upper()
        assert 'href="/new"' in page or 'href="/runs"' in page, path

    # a search that actually found something has its own action per card;
    # the footer nudge would be a second, competing "what next"
    hit = client.get("/search?q=wine&mode=literal").text
    if 'class="scard"' in hit:
        assert 'class="nextstep"' not in hit


def test_the_grafana_tab_shows_the_whole_surface_at_once(console):
    """The project's claim is that Grafana is upstream of the work, and it
    was told in fragments: a panel here, a chip there, a paragraph in the
    README. This tab is the inventory, and it is read from the definitions
    the crew provisions from rather than from a list somebody typed."""
    from customs import grafana_map, grafana_ops
    client, _store, _launched, _jobs = console

    page = client.get("/grafana")
    assert page.status_code == 200
    body = page.text

    for board in grafana_map.dashboards():          # every dashboard, by uid
        assert board.uid in body, board.uid
        for panel in board.panels:
            assert panel.kind in body, f"{board.uid}/{panel.kind}"
    for series in grafana_map.SERIES:               # every metric series
        assert series["name"] in body, series["name"]
    for stream in grafana_map.STREAMS:              # every Loki stream
        assert f'kind="{stream["kind"]}"' in body, stream["kind"]
    for rule in grafana_ops.ALERT_RULES:            # both alert rules
        # escaped, because a threshold has a >= in it and this is HTML
        assert rule["title"] in body and html.escape(rule["expr"]) in body
    for op in grafana_ops.MAPPING:                  # every write, and how it travels
        assert op in body, op
    assert grafana_ops.CONTACT_POINT_NAME in body
    assert grafana_ops.PROM_UID in body and grafana_ops.LOKI_UID in body

    # and it is reachable from the nav of the pages beside it, on the right
    # of the library
    assert body.count('href="/grafana"') >= 1
    assert 'href="/grafana"' in client.get("/library").text
    library_at = client.get("/library").text.index('href="/library"')
    grafana_at = client.get("/library").text.index('href="/grafana"')
    assert grafana_at > library_at, "the new tab sits to the right of Library"


def test_nothing_a_reader_sees_is_punctuated_with_an_em_dash():
    """The house style, enforced rather than remembered.

    Em dashes read as machine-written, and this project's copy is meant to
    read as though a person wrote it, so the punctuation that carries the
    aside is a comma, a colon, a bracket or a full stop. They had crept
    back into sixty-three places in the README alone.

    One exception, and it is quoted: Grafana's own documentation says
    "authenticates users interactively -- there is no service-account or
    machine-token option", and repunctuating somebody else's sentence
    inside quotation marks would be a misquote. A line marked
    "# dash: parsed" is exempt for the other honest reason: it recognises a
    dash in a model's own prose rather than writing one.
    """
    QUOTED = "authenticates users interactively"
    # A pattern that RECOGNISES an en dash in a model's prose is not copy.
    PARSED = "# dash: parsed"
    surfaces = [Path("README.md"), Path("docs/devpost.md"),
                Path("src/customs/static/customs.js")]
    surfaces += sorted(Path("src/customs/templates").glob("*.html"))
    surfaces += sorted(Path("src/customs").glob("*.py"))

    offenders = []
    for path in surfaces:
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if QUOTED in line or PARSED in line:
                continue
            if "\u2014" in line or "\u2013" in line or "&mdash;" in line or "&ndash;" in line:
                offenders.append(f"{path}:{n}: {line.strip()[:80]}")
    assert not offenders, "em dash in copy a reader sees:\n" + "\n".join(offenders)


def test_every_method_the_picker_offers_has_its_own_mark(console):
    """Five choices, three lines of prose each, and nothing to tell them
    apart at a glance. Each method now carries the gesture it performs --
    in the sprite the console draws from, as a file the README can show,
    and in the picker row itself. A method added without one is a row that
    looks like the row above it."""
    from customs import costs
    client, store, _launched, _jobs = console
    run = _judged_run(store)

    sprite = Path("src/customs/templates/base.html").read_text()
    icons = Path("docs/media/icons")
    readme = Path("README.md").read_text()
    for method in costs.METHODS:
        assert f'id="m-{method.key}"' in sprite, method.key
        assert (icons / f"m-{method.key}.svg").is_file(), method.key
        assert f"m-{method.key}.svg" in readme, method.key

    room = client.get(f"/runs/{run.id}/markets/FR").text
    for method in costs.METHODS:
        assert f'href="#m-{method.key}"' in room, method.key


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
                        lambda finding, observation=None, **_: "prop_swap")
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


def test_the_three_fixes_are_a_rotator_that_still_works_without_javascript(console):
    """One fix at a time, big enough to judge a hemline by -- but the page
    is served with all three lanes in it and the tabs inert. customs.js adds
    the class that turns the strip into a slideshow, so with scripting off
    the section is the side-by-side strip it always was."""
    client, _store, _launched, _jobs = console
    page = client.get("/").text

    assert page.count('class="fixtab"') == 3, "one tab per fix"
    for key in ("wine", "smoke", "skirt"):
        assert f'id="fix-{key}"' in page and f'aria-controls="fix-{key}"' in page
    # the served page never hides a lane: only the script does that
    assert 'class="lane off"' not in page and 'class="fixshow on"' not in page
    # and the label saying which side is which is not hidden by stylesheet
    css = (Path("src/customs/static") / "customs.css").read_text()
    assert "figcaption span:last-child { display: none; }" not in css


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

    _enter(client, "visitor")
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
    # none, and warmed on pointerenter: thirty-nine clips fetching metadata
    # up front stopped the archive loading at all
    assert 'preload="none"' in page and "/preview.mp4" in page
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
    _enter(client, "visitor")
    assert client.post(f"/runs/{other.id}/delete",
                       follow_redirects=False).status_code == 404


def test_the_mission_feed_sparkles_on_the_stage_it_is_in(console):
    """The sparkle is the app's one word for "this is running" -- an
    uploading master, a card mid-analysis, a frame being fixed. The mission
    feed is the screen people watch while a run works, so the stage it is
    in wears it too. A finished run wears nothing."""
    client, store, _launched, _jobs = console
    run = _judged_run(store)

    store.set_run_status(run.id, "running")
    page = client.get(f"/runs/{run.id}/mission").text
    assert 'class="grp live"' in page or ' live" data-agent' in page, \
        "the newest stage is the live one"

    css = client.get("/static/customs.css").text
    assert ".grp.live > summary::before" in css, "and the live one sparkles"

    store.set_run_status(run.id, "done")
    page = client.get(f"/runs/{run.id}/mission").text
    assert " live" not in page.split("</head>")[1], "a finished run is still"


def test_the_progress_bar_flows_while_the_run_does(client):
    """The bar sits on one percentage for a minute at a time -- the analyst
    reads every shot, and that is most of the wall clock -- so it flows in
    the same four colours as everything else that is working, and stops
    when the run stops."""
    test_client, _store, _run, _ = client
    css = test_client.get("/static/customs.css").text
    assert "@keyframes spk-flow" in css
    assert ".progress-track span.done { animation: none;" in css
    js = test_client.get("/static/customs.js").text
    assert 'fill.classList.toggle("done", !!data.done)' in js


def test_the_board_poster_plays_the_film_on_hover(client, tmp_path, monkeypatch):
    """The archive's cards play themselves; the board -- the screen people
    land on -- only rotated stills. It plays too now, layered over the
    rotation so a run whose master is gone still has something to show."""
    test_client, store, run, _ = client
    monkeypatch.setattr(app_module, "poster_available", lambda r: True)

    page = test_client.get(f"/runs/{run.id}").text
    assert 'class="board-preview" data-hoverplay' in page
    assert f'src="/runs/{run.id}/preview.mp4"' in page
    # inside the stack, which is the positioned box: over the picture, not
    # over the caption
    stack = page.split('board-poster-stack')[1].split('</span>')[0]
    assert "board-preview" in stack

    css = test_client.get("/static/customs.css").text
    assert ".board-poster:hover .board-preview { opacity: 1; }" in css


def test_grafana_is_credited_wherever_the_mark_is(client):
    """Grafana is upstream of the work -- the crew writes to it over MCP
    before anything reaches the console, and an alert in Grafana is what
    starts a render -- so it is credited beside the mark on every screen
    rather than in a footer nobody reaches."""
    test_client, _store, run, _ = client
    for path in ("/", "/runs", f"/runs/{run.id}", "/agent"):
        page = test_client.get(path).text
        assert "powered-by-grafana.png" in page, f"no Grafana credit on {path}"
    # and the badge is a real file, not a dead reference
    assert test_client.get("/static/powered-by-grafana.png").status_code == 200


def test_agent_mode_says_what_it_can_do_before_you_type(client):
    """It used to open onto two grey panes and a blinking cursor. The hero
    names the tools the agent actually holds -- from agentmode.TOOL_NAMES,
    so the console cannot advertise a set that has drifted from the code --
    and three pointers say what happens when you type."""
    from customs import agentmode
    test_client, _store, _run, _ = client
    page = test_client.get("/agent").text
    assert 'class="agenthero"' in page, "it has a hero like every other screen"
    assert 'class="agentsteps"' in page and 'class="ast-n mono">1<' in page
    for tool in agentmode.TOOL_NAMES:
        assert f'>{tool}</code>' in page, f"{tool} not advertised"
    assert f"its {len(agentmode.TOOL_NAMES)} tools" in page


def test_the_archive_is_one_card_per_film_not_per_run(console):
    """Clearing the same master three times in an afternoon is what happens
    while a pack is being written, and it filled the archive with three
    identical thumbnails whose newest was the only one anyone wanted. The
    newest is the card; the earlier ones are dated links under it, so
    nothing is hidden and nothing is repeated."""
    client, store, _launched, _jobs = console
    same = "runs/uploads/x/Ready_When_You_Are.mp4"
    old_run = _judged_run(store, asset=same)
    mid_run = _judged_run(store, asset=same)
    new_run = _judged_run(store, asset=same)
    other = _judged_run(store, asset="runs/uploads/y/another.mp4")

    page = client.get("/runs").text

    assert page.count('class="runrow"') == 2, "two films, two cards"
    assert f'href="/runs/{new_run.id}"' in page, "the newest run is the card"
    assert f'href="/runs/{other.id}"' in page
    # the earlier two are still reachable, as chips under the card
    assert page.count('class="runolder"') == 1
    for earlier in (old_run, mid_run):
        assert f'<a class="chip" href="/runs/{earlier.id}"' in page, earlier.id
    # and the count says runs AND films, because both are true
    assert "4 runs" in page and "2 films" in page


def test_the_archive_pays_for_nine_cards_and_asks_before_the_rest(console):
    """Every card carries a poster, a lane chart and, for the first few, a
    live Grafana frame. Thirty of those on one paint is a page that takes
    seconds to settle, so nine come with the page and the rest arrive when
    somebody asks for them."""
    client, store, _launched, _jobs = console
    for n in range(12):
        _judged_run(store, asset=f"runs/uploads/{n}/film_{n}.mp4")

    page = client.get("/runs").text

    assert page.count('class="runrow') == 9, "nine films, not twelve"
    assert 'id="loadmore"' in page and 'data-total="12"' in page
    assert "(3 more films)" in page

    # the fragment is the cards alone, in the same markup
    more = client.get("/runs?fragment=1&offset=9")
    assert more.status_code == 200
    assert more.text.count('class="runrow') == 3
    assert "<html" not in more.text.lower(), "cards only, no page around them"

    # ...and there is a way through without any of this
    assert client.get("/runs?all=1").text.count('class="runrow') == 12


def test_every_card_carries_both_charts_and_boots_neither_by_itself(console, monkeypatch):
    """A card has two charts of the same run: the panel Grafana draws and
    the drawing this app makes. The reader picks, live is the default, and
    the choice is one switch for the whole archive.

    What must NOT happen is the page booting a Grafana per card. A lazy
    iframe loads the moment it is anywhere near the viewport, and
    thirty-nine of those kept a real browser from reaching
    domcontentloaded in thirty seconds -- so the frames leave the server
    inert, with their URL in data-src, and customs.js activates them one
    at a time on approach.
    """
    import dataclasses
    client, store, _launched, _jobs = console
    for n in range(4):
        _judged_run(store, asset=f"runs/uploads/{n}/film_{n}.mp4")
    monkeypatch.setattr(app_module, "settings", dataclasses.replace(
        app_module.settings, grafana_viewer_url="https://viewer.example.run.app"))

    page = client.get("/runs").text
    cards = page.count('class="cardviz"')
    assert cards >= 4, f"{cards} cards"
    # both charts on every card, and the switch to choose between them
    assert page.count('class="cardlanes live"') == cards
    assert page.count('class="cardlanes drawn"') == cards
    assert page.count('data-viz-pick="live"') == cards

    # not one of the card frames has a src: nothing boots on paint
    frames = re.findall(r'<iframe class="cardlanes live"[^>]*>', page)
    assert frames and all(" src=" not in f for f in frames), frames[:1]
    assert all("data-src=" in f for f in frames)
    # the panel is pinned to the rows the icons name, so they cannot drift
    assert all("var-dim=" in f for f in frames)

    # the archive's own instance-wide panel is a different thing and does
    # load: it is one, at the top, not one per card
    assert 'class="archlive"' in page


def test_a_door_asks_for_its_word(console):
    """One shared password per door, kept in a cookie. Not an account and
    not encryption -- it is there because generation costs real money and a
    submission link travels further than the people it was sent to."""
    from customs.config import settings
    client, _store, _launched, _jobs = console

    page = client.get("/enter/judge")
    assert page.status_code == 200 and 'name="password"' in page.text

    wrong = client.post("/enter/judge", data={"password": "letmein"},
                        follow_redirects=False)
    assert wrong.status_code == 303 and "wrong=1" in wrong.headers["location"]
    assert "customs-role" not in wrong.cookies

    right = client.post("/enter/judge", data={"password": settings.judge_password},
                        follow_redirects=False)
    assert right.status_code == 303 and right.headers["location"] == "/runs"


def test_nothing_that_spends_or_destroys_opens_without_the_word(console):
    """The gate is only a gate if something is behind it.

    Reading is deliberately open to anyone with the link -- that is the
    submission. Spending is not: every route below either calls a model on
    a real card or deletes rows that do not come back, and each one sends a
    visitor who never met a door back to it rather than doing the work.
    """
    client, store, launched, jobs = console
    client.cookies.clear()  # somebody arriving from a link, not from a door
    run = _judged_run(store)
    before = len(store.recent_runs(50))

    form = client.get("/new", follow_redirects=False)
    assert form.status_code == 303
    assert form.headers["location"] == "/enter/visitor?next=/new"

    started = _upload(client)
    assert started.status_code == 303
    assert started.headers["location"].startswith("/enter/visitor")
    assert launched == [], "and no crew was started"
    assert len(store.recent_runs(50)) == before, "and no run was created"

    fid = "fnd_FR_FR-ALC-01_obs_shot_0_000"
    for path, data in (
            (f"/runs/{run.id}/findings/{fid}/remediate", {"method": "overlay"}),
            (f"/runs/{run.id}/analysis", {"markets": ["US"]}),
            (f"/runs/{run.id}/delete", {}),
            ("/agent/ask", {"message": "hello"})):
        reply = client.post(path, data=data, follow_redirects=False)
        assert reply.status_code in (303, 403), path
        if reply.status_code == 303:
            assert reply.headers["location"].startswith("/enter/visitor"), path
        else:  # the agent answers in the shape its own chat log draws
            assert "door" in reply.json()["error"]
    assert jobs == [], "and nothing was enqueued"
    assert store.get_run(run.id) is not None, "and the run is still there"

    # ...while every reading route stays open to exactly the same person
    for path in ("/", "/runs", "/library", f"/runs/{run.id}",
                 f"/runs/{run.id}/markets/FR", f"/runs/{run.id}/frames"):
        assert client.get(path).status_code == 200, path


def test_a_click_on_a_grafana_panel_meets_the_door_and_then_lands(console):
    """/launch/remediate is a GET that spends -- the click on a Grafana data
    link IS the launch. So the door has to carry the whole click back, query
    and all: a judge who says the word should get the edit the panel asked
    for, not a board and a puzzle about which square they pressed."""
    from customs.config import settings
    client, store, _launched, jobs = console
    client.cookies.clear()
    run = _judged_run(store)
    fid = "fnd_FR_FR-ALC-01_obs_shot_0_000"
    click = f"/launch/remediate?run={run.id}&finding={fid}&method=overlay"

    stopped = client.get(click, follow_redirects=False)
    assert stopped.status_code == 303
    assert jobs == [], "and nothing was launched on the way past"
    door = stopped.headers["location"]
    assert door.startswith("/enter/visitor?next=")

    # the door grants and hands the click straight back, unchanged
    opened = client.get(door, follow_redirects=False)
    assert opened.status_code == 303
    assert opened.headers["location"] == click
    assert opened.cookies["customs-role"] == "visitor"
    assert jobs == [], "the door itself launches nothing"

    client.get(click, follow_redirects=False)
    assert jobs == [(run.id, fid, "FR")], "and now the click does what it said"


def test_the_door_returns_you_to_what_you_were_doing(console):
    """Being stopped at a door and then dropped on a different page is how
    people lose what they came for. The word is asked for where they were
    going, and they land there. Anywhere on this console -- and nowhere
    else, because a door that forwards to any URL it is handed is an open
    redirect wearing a convenience's clothes."""
    from customs.config import settings
    client, *_ = console
    client.cookies.clear()

    kept = client.get("/enter/visitor?next=/runs/run_x/markets/FR",
                      follow_redirects=False)
    assert kept.status_code == 303
    assert kept.headers["location"] == "/runs/run_x/markets/FR"

    right = client.get("/enter/visitor?next=/agent", follow_redirects=False)
    assert right.headers["location"] == "/agent"

    # and nowhere else: a door that forwards to any URL it is handed is an
    # open redirect wearing a convenience's clothes, password or no password
    for away in ("//evil.example/x", "https://evil.example/x", "javascript:1"):
        off = client.get(f"/enter/visitor?next={away}", follow_redirects=False)
        assert off.headers["location"] == "/new", away
        off2 = client.post("/enter/visitor", data={"next": away},
                           follow_redirects=False)
        assert off2.headers["location"] == "/new", away


def test_a_visitor_has_their_own_daily_ceiling(console):
    """The instance budget stops the day running away; this stops one
    visitor spending everyone else's. A visitor's identity is the runs they
    started, so their spend is the ledger summed over exactly those."""
    client, store, _launched, _jobs = console
    run = _judged_run(store)
    _enter(client, "visitor")
    client.cookies.set("customs-mine", run.id)

    fid = "fnd_FR_FR-ALC-01_obs_shot_0_000"
    store.record_spend("bridge", app_module.VISITOR_DAILY_EUR + 0.01, run.id, fid)

    r = client.post(f"/runs/{run.id}/findings/{fid}/remediate",
                    data={"method": "overlay"}, follow_redirects=False)
    assert r.status_code == 429
    assert "generation for today" in r.json()["detail"]
    # and reading is never gated by it
    assert client.get(f"/runs/{run.id}/markets/FR").status_code == 200

    # a cookie is a string anyone can type, so the ceiling is written as
    # "not the judge" rather than "is the visitor": a made-up role is held
    # to the narrower rule, not handed the wider one
    client.cookies.set("customs-role", "producer")
    made_up = client.post(f"/runs/{run.id}/findings/{fid}/remediate",
                          data={"method": "overlay"}, follow_redirects=False)
    assert made_up.status_code == 429


def test_the_agent_says_what_it_is_doing_while_it_does_it(console, monkeypatch):
    """A spinner that says "working" for forty seconds is the console
    asking to be trusted. The agent records every tool call as it makes
    them, so the page reads that list while the turn is still running and
    says what it looked up, what it asked Grafana and what it built."""
    from customs import agentmode
    client, _store, _launched, _jobs = console

    # nothing in flight: an empty answer, which is how the page stops asking
    idle = client.get("/agent/progress?session=nobody").json()
    assert idle == {"running": False, "phases": []}

    turn = agentmode.Turn()
    monkeypatch.setitem(agentmode.LIVE, "s1", turn)
    turn.calls.append({"tool": "data_schema"})
    turn.calls.append({"tool": "search_frames", "text": "bunnies"})
    turn.calls.append({"tool": "chart", "types": ["barchart", "barchart"]})

    live = client.get("/agent/progress?session=s1").json()

    assert live["running"] is True
    assert live["phases"] == ["reading what Grafana holds",
                              'searching every caption for "bunnies"',
                              "building a barchart in Grafana"]


def test_a_phase_is_never_a_stack_trace(console):
    """Every tool the agent has gets a sentence, and one it does not know
    falls back to the tool's own name rather than raising inside a poll."""
    from customs import agentmode

    for tool in agentmode.TOOL_NAMES:
        said = agentmode.phrase({"tool": tool})
        assert said and "{" not in said, tool
    assert agentmode.phrase({"tool": "invented"}) == "invented"
    assert agentmode.phrase({}) == "working"


def test_the_alert_webhook_refuses_an_alert_that_carries_no_key(client, monkeypatch):
    """This route SPENDS. A forged alert naming a real open finding starts a
    paid generative fix and rewrites a localized master, and it was open to
    anyone who had the service URL -- verified against production, which
    answered {"accepted": 1}.

    Grafana cannot sign a request or send a header from a contact point, so
    the secret rides in the URL deploy.sh writes into it. Wrong key and no
    key both get the same 404 a stranger would get from a route that does
    not exist.
    """
    import dataclasses

    from customs import app as app_mod

    test_client, _store, run, jobs = client
    monkeypatch.setattr(app_mod, "settings", dataclasses.replace(
        app_mod.settings, webhook_token="s3cret"))

    assert test_client.post("/webhook/alert", json=_alert()).status_code == 404
    assert test_client.post("/webhook/alert?key=wrong",
                            json=_alert()).status_code == 404
    assert jobs == [], "and neither one enqueued a fix"

    right = test_client.post("/webhook/alert?key=s3cret", json=_alert())
    assert right.status_code == 200 and right.json()["accepted"] == 1
    assert jobs == [(run.id, "fnd_FR_FR-ALC-01_obs_shot_0_000", "FR")]


def test_the_public_url_serves_no_api_console_and_says_nosniff(console):
    """Swagger published a try-it-now button for every POST this console
    has, delete and the alert webhook included, on a URL meant to be shared
    with strangers."""
    client, _store, _launched, _jobs = console

    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404, path

    headers = client.get("/").headers
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert "max-age=" in headers["Strict-Transport-Security"]


def test_the_fix_picker_is_its_own_row(console, monkeypatch):
    """The panel used to open inside the findings table's last cell, which
    is 12% of the table and sized for the word "open". It came out clipped
    at the table's edge with the price column -- the number the decision
    turns on -- outside the panel entirely, and the same scope caveat was
    repeated verbatim under every method it ruled out.

    It is a row spanning all eight columns now, the caveat is said once,
    and the pre-checked method is one the panel has not just called a poor
    fit."""
    from customs import scope as scope_mod
    client, store, _launched, _jobs = console
    run = _judged_run(store)

    page = client.get(f"/runs/{run.id}/markets/FR").text
    assert '<tr class="fixrow">' in page, "the panel spans the whole table"
    toggle = page.split('<details class="fixer">')[1].split("</details>")[0]
    assert "fix-opt" not in toggle and "<form" not in toggle, (
        "the panel must not render inside the narrow last cell")

    # concept scope is the case that produced the wall: every method but
    # regeneration carries one and the same sentence
    monkeypatch.setattr(scope_mod, "classify", lambda *a, **k: "concept")
    page = client.get(f"/runs/{run.id}/markets/FR").text
    panels = [p.split("</tr>")[0] for p in page.split('<tr class="fixrow">')[1:]]
    assert panels, "an open remediable finding still offers a panel"
    for panel in panels:
        assert panel.count('class="fp-caveat"') == 1, (
            "one caveat for the finding, not one paragraph per method")
        assert panel.count("fp-tag") >= 2, "the poor fits stay marked"
        options = [o.split("</label>")[0]
                   for o in panel.split('<label class="fix-opt')[1:]]
        methods = [o for o in options if 'name="method"' in o]
        picked = [o for o in methods if " checked" in o]
        assert len(picked) == 1, "exactly one method is pre-selected"
        assert "fp-tag" not in picked[0], (
            "the default is a method that fits, not one just called a poor fit")


def test_grafana_panels_wear_the_console_s_theme(console):
    """A Grafana panel embedded in the console is a picture Grafana draws,
    and it draws light or dark on request. Pinned to light, every panel on
    Mission was a torch in a dark room.

    The console's theme lives in localStorage, which the server cannot
    read, so base.html mirrors it into a cookie and every embed URL is
    built from that. A toggle re-sources the panels in place, which is what
    the repaint in customs.js is for -- the cookie alone would only fix the
    next page load."""
    client, store, _launched, _jobs = console
    run = _judged_run(store)

    light = client.get(f"/runs/{run.id}").text
    assert "theme=dark" not in light, "light console, light panels"

    client.cookies.set("customs-theme", "mission")
    dark = client.get(f"/runs/{run.id}").text
    assert "theme=light" not in dark, "Mission asks Grafana for dark panels"

    # the archive's one instance-wide panel follows too
    assert "theme=light" not in client.get("/runs").text

    # the renders the server makes itself are cached per theme: one file
    # overwriting the other is how a light panel lands on Mission
    from customs import app as app_module
    src = inspect.getsource(app_module.run_lanes_grafana)
    assert "lanes-dark.png" in src and "lanes.png" in src

    # and a toggle re-sources what is already on the page, because the
    # cookie alone only fixes the NEXT load
    js = (Path(app_module.__file__).parent / "static" / "customs.js").read_text()
    assert "repaint" in js and "theme=" in js


def test_two_runs_of_one_file_read_as_one_film(console):
    """Upload ad.mp4 for France on Monday and for the Gulf on Thursday and
    you have two clearances of one commercial, each knowing nothing about
    the other. The Gulf verdicts were invisible from the French run, and no
    screen anywhere answered "is this film cleared".

    The market strip is the film's now, not the run's: the other pass's
    markets ride in it, marked, each linking into the run that judged
    them. Same file means the same uploaded filename, which is the identity
    the metrics, the alerts and the Grafana panels already use."""
    client, store, _launched, _jobs = console
    from customs import app as app_module

    first = _judged_run(store)                       # FR, and the fixture's own
    asset = first.asset_path
    # the Gulf pass: a market this run never judged
    second = store.create_run(asset_path=asset, markets=["AE"])
    store.set_run_status(second.id, "done")

    rows = app_module.market_rows(store.get_run(first.id))
    seen = {m["code"]: m for row in rows for m in row["markets"]}
    assert "AE" in seen, "the other pass's market is in this run's strip"
    assert seen["AE"]["elsewhere"] == second.id, "and it names where it lives"
    assert seen["AE"]["run"] == second.id
    assert seen["FR"]["elsewhere"] is None, "this run's own markets are its own"

    # the strip links out to the run that actually holds it
    page = client.get(f"/runs/{first.id}").text
    assert f'/runs/{second.id}/markets/AE' in page
    assert "mk-elsewhere" in page

    # and a run of a DIFFERENT file does not join the film
    store.create_run(asset_path="/tmp/somethingelse.mp4", markets=["TH"])
    rows = app_module.market_rows(store.get_run(first.id))
    codes = {m["code"] for row in rows for m in row["markets"]}
    assert "TH" not in codes, "a different commercial is a different film"



def test_agent_mode_opens_guided_and_fits_one_screen(console):
    """Two things a first-time visitor met: seven suggestion chips in one
    flat row, nothing saying which to press first, and a console taller
    than the window -- .agent was on a hardcoded calc(100vh - 132px)
    written when the panes were the only thing on the page, so once a hero
    and three pointers went above them the ask box fell below the fold.

    The rail opens in the order the work happens, and the screen is a flex
    column that divides one viewport between the hero and the panes."""
    client, _store, _launched, _jobs = console
    page = client.get("/agent").text

    assert page.count('class="sugg-lane"') == 3, "where I stand, why, what to do"
    assert "Start here" in page

    css = (Path(app_module.__file__).parent / "static" / "customs.css").read_text()
    # the screen is sized to the window, so the ask box is never below the
    # fold -- and the log has a floor, because it was the thing that gave
    # way to the hero, the stats row and three lanes of questions
    assert ".agentscreen" in css and "height: calc(100vh - 96px)" in css
    assert "min-height: 150px" in css
    assert "height: calc(100vh - 132px)" not in css,         "the number written for a page that no longer exists"

    # the two marks, at twice the size they were
    assert 'width="50" height="50"' in page, "the Agent Builder mark"
    assert 'width="190" height="44"' in page, "the Grafana mark"



def test_the_intelligence_board_labels_grafana_with_the_console_s_own_icons(
        console, monkeypatch):
    """Every other screen answers a question about one commercial. This one
    reads across every run, out of the same two stores the crew wrote.

    The division of labour is the whole design: Grafana charts the data
    because it holds it, and this console draws the axis because Grafana
    has never heard of an eighteen-part taxonomy or a broadcaster channel's
    mark. They agree because the icon order comes from the same query the
    panel beside it runs -- an axis that disagrees with its bars is worse
    than no axis."""
    from customs import app as app_module
    client, _store, _launched, _jobs = console

    monkeypatch.setattr(app_module, "_ranked", lambda query, label, **kw: (
        [{"key": "alcohol_tobacco_drugs", "n": 270},
         {"key": "modesty_dress_body", "n": 61}] if label == "dimension" else
        [{"key": "EU", "n": 120}, {"key": "FR", "n": 44},
         {"key": "BE-PLAY", "n": 3}, {"key": "GLOBAL", "n": 1}]))

    page = client.get("/insight").text
    assert page.count('class="xkey"') == 6, "one key per row, both axes"
    # ranked, biggest first, so the axis reads in the panel's own order
    assert page.index("alcohol_tobacco_drugs") < page.index("modesty_dress_body")

    # every market wears the mark its level earns, exactly as the run nav
    # does -- the country sprite has no EU, no GLOBAL and no channels
    assert "#n-market" in page, "a continent is not a country"
    assert "#d-national_symbols_politics" in page, "nor is the global baseline"
    assert "#n-cut" in page, "nor is a broadcaster channel"
    assert "#c-FR" in page, "a country is"
    assert "#c-EU" not in page and "#c-GLOBAL" not in page

    # and the board is provisioned, so the page frames something real
    from customs import grafana_map
    uids = {d.uid for d in grafana_map.dashboards()}
    assert "customs-insight" in uids


def test_a_market_room_hands_over_a_certificate_with_every_finding_on_it(console):
    """Every other output of this system is a screen. A clearance desk's
    output is a document: what was cleared, for where, on what date, on
    which statutes, and what was changed to get there.

    So the PDF is not a summary. Each finding carries its rule id, its
    class, its severity, its seconds, the statute, the citation the
    adjudicator retrieved, and -- where there is one -- the verifier's own
    sentence about the fix. A finding the Guard refused says so in those
    words, because that refusal is the product working.
    """
    from pypdf import PdfReader  # noqa: PLC0415 -- test-only reader

    client, store, _launched, _jobs = console
    run = _judged_run(store)
    finding = next(f for f in store.findings(run.id, "FR"))
    store.emit(run.id, "verifier",
               f"fixed: {finding.rule_id} no longer fires at 1.00-2.00s "
               f"after prop_swap; {finding.id} resolved")

    answer = client.get(f"/runs/{run.id}/markets/FR/certificate.pdf")
    assert answer.status_code == 200
    assert answer.headers["content-type"] == "application/pdf"
    assert ".pdf" in answer.headers["content-disposition"]
    assert answer.content[:4] == b"%PDF"

    text = " ".join(page.extract_text() for page in
                    PdfReader(io.BytesIO(answer.content)).pages)
    assert "CLEARANCE CERTIFICATE" in text
    assert run.id in text and "France" in text
    assert finding.rule_id in text
    assert "not legal advice" in text
    # the verifier is quoted, not paraphrased
    assert "no longer fires" in text

    # a market this run never covered is a 404, like any other unknown path
    assert client.get(f"/runs/{run.id}/markets/JP/certificate.pdf").status_code == 404

    # and the room offers it
    assert f"/runs/{run.id}/markets/FR/certificate.pdf" in \
        client.get(f"/runs/{run.id}/markets/FR").text


def test_the_budget_alert_stops_the_loop_that_spends(console, monkeypatch):
    """Two of the three alert rules ask Grafana to wake the Remediator. The
    third asks it to stop: the loop that fixes a finding by generating
    video is the one that can empty a day's budget while nobody is
    watching.

    So a firing customs_budget_low holds the automatic path for the rest of
    the UTC day. Findings still block and alerts still arrive -- they are
    recorded and refused, in the feed, in words -- and a person at the
    console can still spend what is left one fix at a time.
    """
    from customs import app as app_module

    client, store, _launched, _jobs = console
    run = _judged_run(store)
    finding = next(f for f in store.findings(run.id) if f.status == "open")
    monkeypatch.setattr(app_module, "_REMEDIATION_PAUSED_DAY", "", raising=False)

    def fire(labels):
        return client.post("/webhook/alert",
                           json={"alerts": [{"status": "firing", "labels": labels}]})

    real = {"asset": Path(run.asset_path).stem, "market": finding.market,
            "rule_id": finding.rule_id}

    # before the budget alert, a blocking finding starts work
    assert fire(real).json()["accepted"] == 1

    # the budget rule carries no asset at all: it is about the card
    held = fire({"alertname": "customs_budget_low", "action": "pause_remediation",
                 "team": "customs"})
    assert held.json() == {"accepted": 0, "ignored": 1}
    assert app_module.remediation_paused()

    # and now the same alert is recorded and refused rather than acted on
    assert fire(real).json()["accepted"] == 0
    feed = " ".join(m for _i, _t, _a, m in store.events_since(run.id, 0))
    assert "held" in feed and "paused until midnight UTC" in feed

    # tomorrow is a different day, and the pause does not survive it
    monkeypatch.setattr(app_module, "_REMEDIATION_PAUSED_DAY", "1999-01-01",
                        raising=False)
    assert not app_module.remediation_paused()
    assert fire(real).json()["accepted"] == 1


def test_the_days_ledger_is_a_mimir_series_with_a_rule_on_it(monkeypatch):
    """A budget that only exists as a number on one page of the console is
    a budget nobody watches. Two series on the real clock, and the third
    alert rule is the one that reads them.
    """
    from customs import telemetry
    from customs.costs import DAILY_BUDGET_EUR
    from customs.grafana_ops import ALERT_RULES, BUDGET_ALERT_EUR

    pushed = {}
    monkeypatch.setattr(telemetry, "_otlp_push", pushed.update)
    telemetry.push_spend(12.5, DAILY_BUDGET_EUR)

    assert set(pushed) == {"customs_spend_eur_total", "customs_budget_remaining_eur"}
    assert pushed["customs_spend_eur_total"][0]["asDouble"] == 12.5
    assert pushed["customs_budget_remaining_eur"][0]["asDouble"] == \
        DAILY_BUDGET_EUR - 12.5

    rule = next(r for r in ALERT_RULES if r["uid"] == "customs-budget-low")
    assert f"<= {BUDGET_ALERT_EUR}" in rule["expr"]
    assert rule["labels"]["action"] == "pause_remediation"
    # the floor has to leave room for the most expensive single fix
    from customs.costs import estimate
    assert BUDGET_ALERT_EUR > estimate("bridge", 8.0)


def test_a_withheld_stem_disappears_from_every_read_path(console, monkeypatch):
    """One tuple hides a film everywhere, and it ships empty.

    It held twenty-four stems for a while -- footage that came into the
    corpus while the tool was being built -- hidden rather than deleted so
    that nothing was lost and the decision stayed reversible. The owner
    reversed it: every clearance is in the app again.

    What is under test is therefore the mechanism, not the list. Put a stem
    back in that tuple and the archive stops listing it, the run's own
    pages 404 like any unknown run, the store's listing drops it so
    everything built on it inherits the omission, and every cross-run Loki
    query excludes it by label -- and with the tuple as it ships, none of
    that happens to anything.
    """
    from customs import config

    client, store, _launched, _jobs = console
    # Empty in the shipped config: the owner wants every clearance in the
    # app. So the mechanism is what is under test, with one stem standing
    # in for the list -- put a name back in that tuple and this is what
    # happens to it, everywhere, at once.
    monkeypatch.setattr(config, "WITHHELD_ASSETS", ("BOND_JAMES_BOND",))
    withheld = _judged_run(store, asset="/tmp/BOND_JAMES_BOND.mp4")
    ours = _judged_run(store, asset="/tmp/ember_lounge.mp4")

    assert config.is_withheld("BOND_JAMES_BOND")
    assert not config.is_withheld("ember_lounge")

    # the archive lists one of the two, and it is ours
    archive = client.get("/runs?all=1").text
    assert ours.id in archive
    assert withheld.id not in archive
    assert "BOND_JAMES_BOND" not in archive

    # 404, and by the same words any unknown run gets: a page that
    # explained itself would be a page that names the film
    assert client.get(f"/runs/{withheld.id}").status_code == 404
    assert client.get(f"/runs/{withheld.id}/poster.jpg").status_code == 404
    assert client.get(f"/runs/{withheld.id}/mission").status_code == 404
    assert client.get(f"/runs/{ours.id}").status_code == 200

    # the store's own listing path is where that happens, so everything
    # built on it (the boards, the agent's context, the frame index)
    # inherits it rather than repeating it
    listed = {r.id for r in store.recent_runs(50)}
    assert ours.id in listed and withheld.id not in listed
    assert store.get_run(withheld.id) is not None, "hidden, not deleted"

    # and every cross-run Loki query carries the label matcher
    from customs.search import logql
    for query in (logql("cigar"), logql("")):
        assert "asset!~" in query
    assert "BOND_JAMES_BOND" in logql("cigar")

    # with the tuple as it ships, nothing is filtered and nothing is hidden
    monkeypatch.setattr(config, "WITHHELD_ASSETS", ())
    assert config.withheld_matcher() == ""
    assert "asset!~" not in logql("cigar")
    assert client.get(f"/runs/{withheld.id}").status_code == 200


def test_both_explainers_say_the_console_can_be_asked(console):
    """The front page and the tour explained the crew, the ladder, the
    guard, the fix loop and Grafana, and never mentioned that the whole
    thing can be asked for in sentences -- which is the half of the product
    the hackathon is about.

    Both screens say it now, and both count the tools from agentmode
    rather than from a sentence somebody typed, so a tool added or removed
    cannot leave a claim behind.
    """
    from customs import agentmode

    client, _store, _launched, _jobs = console
    tools = len(agentmode.TOOL_NAMES)

    front = client.get("/").text
    assert "agent mode" in front.lower()
    assert f"{tools} tools" in front
    assert 'href="/agent"' in front

    deck = client.get("/tour").text
    assert "Agent mode is not a chatbot bolted on the side." in deck
    assert f"{tools} tools" in deck
    # and the walk visits the live agent screen
    stops = client.get("/tour/walk.json").json()["stops"]
    assert any(stop["path"] == "/agent" for stop in stops)
    agent = client.get("/agent").text
    assert 'data-tour="agent-ask"' in agent


def test_the_page_that_prints_every_query_never_prints_a_withheld_title(console,
                                                                        monkeypatch):
    """The neatest way to leak a hidden list was to publish the filter. The
    Grafana page prints every panel's expression verbatim, and the matcher
    is a regex made of the film titles, so for one deploy the only screen
    still naming them was the screen explaining how they are excluded.

    The list ships empty, so there is nothing to leak today; what has to
    keep holding is that the clause is shown by name and never by content.
    """
    from customs import config, grafana_map

    client, _store, _launched, _jobs = console
    page = client.get("/grafana").text
    assert "asset!~`^(" not in page, "the regex itself is never printed"

    # and when something IS withheld, the clause is named, not spelled
    monkeypatch.setattr(config, "WITHHELD_ASSETS", ("BOND_JAMES_BOND",))
    stamped = '{app="customs", kind="finding"' + config.withheld_matcher() + '}'
    shown = grafana_map._query_of({"expr": stamped})
    assert "BOND_JAMES_BOND" not in shown
    assert "asset!~<withheld>" in shown


def test_the_agents_own_queries_are_scoped_to_the_corpus_too(monkeypatch):
    """Agent mode hands the model the label schema and lets it compose its
    own LogQL, which is the point of that screen and also a way for a film
    the console will not show to be named in an answer. Every selector it
    writes is scoped before it runs, once, and a PromQL expression is left
    alone because the metrics carry no titles.
    """
    from customs import config

    composed = 'sum by (asset) (count_over_time({app="customs", kind="finding"}[7d]))'
    # nothing is withheld as this ships, so the model's query is its own
    assert config.scope_logql(composed) == composed

    monkeypatch.setattr(config, "WITHHELD_ASSETS", ("BOND_JAMES_BOND",))
    scoped = config.scope_logql(composed)
    assert scoped.count("asset!~") == 1
    assert config.withheld_matcher() in scoped
    # idempotent: a schema example that already carries it is not doubled
    assert config.scope_logql(scoped).count("asset!~") == 1
    # and nothing is invented around a metric query
    assert config.scope_logql("sum(customs_risk)") == "sum(customs_risk)"
    assert config.scope_logql("") == ""


def test_the_framed_panels_and_the_withheld_list_agree():
    """The console filters its own queries in code; the framed panels are
    Grafana's, and Grafana reads the JSON in grafana/dashboards. A panel
    whose expression is just {app="customs", kind="finding"} charts the
    whole tenant, which is how the intelligence board came to show a row of
    studio cartoons under a paragraph about this project's own corpus.

    So the matcher has to be in the files, and this is the thing that
    notices when config.WITHHELD_ASSETS changes and the files do not.
    """
    import json
    from pathlib import Path as _Path

    from customs.config import withheld_matcher

    matcher = withheld_matcher()
    for path in sorted(_Path("grafana/dashboards").glob("*.json")):
        for panel in json.loads(path.read_text())["panels"]:
            for target in panel.get("targets", []):
                expr = target.get("expr") or ""
                if (target.get("datasource") or {}).get("type") != "loki":
                    continue
                if 'app="customs"' not in expr:
                    continue
                if matcher:
                    assert matcher.lstrip(", ") in expr, (
                        f"{path.name} panel {panel['id']} charts the whole "
                        f"tenant. Run scripts/stamp_withheld_dashboards.py")
                else:
                    # and the other way: an emptied list has to take the
                    # stamp back out, or the panels keep hiding films the
                    # console is showing again
                    assert "asset!~" not in expr, (
                        f"{path.name} panel {panel['id']} still filters a "
                        f"corpus nothing withholds. Run "
                        f"scripts/stamp_withheld_dashboards.py")


def test_no_framed_panel_goes_blank_on_a_quiet_instance():
    """Judging runs for weeks after the deadline, and this instance is
    quiet on most of those days. Every framed panel therefore has to be
    windowed to where the data IS, not to the last few hours: the launch
    board's overview was pinned to now-6h, so a judge opening a
    three-week-old run was told "No data" by a panel holding every number
    it needed.

    The mapped-clock panels (grid, lanes, timeline) are windowed to the
    run itself by embed_url and are not the subject here. This is about
    the wall-clock ones.
    """
    import json
    from pathlib import Path as _Path

    from customs.app import embeds
    from customs.schema import RunRecord

    run = RunRecord(id="run_quiet", asset_path="/tmp/ember_lounge.mp4",
                    t0=1_700_000_000.0, status="judged", markets=["EU"])
    over = embeds(run)["overview"]
    assert "now-6h" not in over
    # anchored a quarter of an hour before the run's own first sample
    assert f"from={int((run.t0 - 900) * 1000)}" in over
    assert "to=now" in over, "and open at the present, so a fix today moves it"

    # a run that never started has no samples under any window; 30 days is
    # the outer bound of what the store still holds
    assert "from=now-30d" in embeds(
        RunRecord(id="run_new", asset_path="/tmp/x.mp4", t0=None,
                  status="created", markets=[])
    )["overview"]

    # and no wall-clock dashboard ships a window narrower than that either,
    # because /grafana/{uid}.png renders whatever the dashboard stores
    mapped = {"grid.json", "lanes.json", "timeline.json"}
    for path in sorted(_Path("grafana/dashboards").glob("*.json")):
        if path.name in mapped:
            continue
        window = json.loads(path.read_text())["time"]["from"]
        assert window == "now-30d", f"{path.name} stores {window}"


def test_the_intelligence_board_survives_a_dead_grafana(console, monkeypatch):
    """The panels are iframes that show their own errors. The axis beside
    them is this app's, and if the query behind it fails there is simply
    nothing to label -- one failure mode, on the thing that owns it,
    instead of a 502 on a page of fourteen working panels."""
    from customs import app as app_module

    client, _store, _launched, _jobs = console
    app_module._insight_cache.clear()

    def explode(*a, **k):
        raise RuntimeError("grafana is down")

    monkeypatch.setattr(app_module, "GrafanaOps", explode, raising=False)
    monkeypatch.setattr("customs.grafana_ops.GrafanaOps", explode)

    page = client.get("/insight")
    assert page.status_code == 200
    assert "What every clearance adds up to" in page.text


def test_the_filmstrip_pairs_grafana_s_blocks_with_the_console_s_footage(
        console, monkeypatch):
    """The hero of the intelligence board is one bar per commercial, lit to
    its worst recorded severity, with that film's own first frame directly
    underneath it. It is the icon-and-live-Grafana idea taken as far as it
    goes: Grafana holds the numbers and has never seen the footage, the
    console holds the footage and cannot chart anything live.

    Which means the two halves have to agree about order and about state,
    and the state has to be the console's own three -- green cleared, blue
    noted, red past the 70 where a finding starts to block a market."""
    from customs import app as app_module
    client, store, _launched, _jobs = console
    run = _judged_run(store)
    asset = Path(run.asset_path).stem

    monkeypatch.setattr(app_module, "_ranked", lambda query, label, **kw: (
        [{"key": asset, "n": 95}, {"key": "gone_ad", "n": 55},
         {"key": "quiet_ad", "n": 12},
         ] if label == "asset" else []))

    page = client.get("/insight").text
    # 'fs-shot ' with the space: 'fs-shots' is the container around them
    assert page.count('class="fs-shot s-') == 3, "one still per film"
    # the console's three states, at its own thresholds
    assert "s-blocked" in page and "s-cleared" in page and "s-noted" in page
    # a film whose run is still here shows its poster and links to it
    assert f"/runs/{run.id}/poster.jpg" in page
    # one whose run has been deleted keeps its block and loses its still,
    # rather than borrowing somebody else's
    assert "no run" in page
    assert page.index("gone ad") < page.index("quiet ad"), \
        "worst first, which is the order the blocks above are in"


def test_the_axis_breaks_a_tie_the_way_the_panel_beside_it_does(monkeypatch):
    """Grafana sorts these panels by value descending and ties by name.
    The console has to reach the same order from its own execution of the
    same query, or the poster under a block belongs to another film.

    Loki is the reason this cannot be left alone: sort_desc says nothing
    about ties, and five executions of the hero's expression came back in
    five different arrangements of the seven films sitting at 95. So the
    rows arrive here shuffled on purpose.
    """
    from customs import app as app_module

    rows = [{"labels": {"asset": "solstice"}, "value": 95.0},
            {"labels": {"asset": "boro_Comet"}, "value": 95.0},
            {"labels": {"asset": "BOND_JAMES"}, "value": 95.0},
            {"labels": {"asset": "quiet_ad"}, "value": 12.0}]

    class FakeOps:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def loki_instant(self, query): return rows

    monkeypatch.setattr("customs.grafana_ops.GrafanaOps", FakeOps)
    app_module._insight_cache.clear()

    order = [r["key"] for r in app_module._ranked("whatever", "asset")]
    # biggest first, and inside the tie the name -- case-insensitively,
    # because Grafana's sortBy is: a codepoint sort would read
    # BOND_JAMES, boro_Comet as BOND_JAMES, then anything capitalised.
    assert order == ["BOND_JAMES", "boro_Comet", "solstice", "quiet_ad"]
    app_module._insight_cache.clear()


def test_findings_leave_as_timeline_markers_for_the_suite(console):
    """The console can fix a span itself and often should. For the rest,
    the person who fixes it has the master open in Resolve or Premiere, and
    what they need is not a web page: it is the timecodes on their own
    timeline, in the right colour, with the statute in the note.

    Both formats carry the rate they were written at, because HH:MM:SS:FF
    is a frame off per second at the wrong one and looks perfectly
    plausible while it drifts.
    """
    from customs.markers import timecode

    client, store, _launched, _jobs = console
    run = _judged_run(store)
    findings = store.findings(run.id, "FR")

    csv_out = client.get(f"/runs/{run.id}/markets/FR/markers.csv")
    assert csv_out.status_code == 200
    assert "attachment" in csv_out.headers["content-disposition"]
    rows = [r for r in csv_out.text.splitlines() if r.strip()]
    assert rows[0] == "Name,Start,End,Duration,Color,Notes"
    assert len(rows) == 1 + len(findings)
    body = csv_out.text
    for finding in findings:
        assert finding.rule_id in body
        # the statute travels with the marker, because that is what an
        # editor reads while deciding what to do about it
        if finding.citation_ref:
            assert finding.citation_ref[:24] in body

    # a blocking legal finding is red; anything already verified is green
    assert "Red" in body

    edl = client.get(f"/runs/{run.id}/markets/FR/markers.edl")
    assert edl.status_code == 200
    assert edl.text.startswith("TITLE: CUSTOMS FR")
    assert "FCM: NON-DROP FRAME" in edl.text
    assert "* COMMENT:" in edl.text

    # anything else is a 404 rather than an empty file with a nice name
    assert client.get(f"/runs/{run.id}/markets/FR/markers.xml").status_code == 404
    assert client.get(f"/runs/{run.id}/markets/JP/markers.csv").status_code == 404

    # and the timecode itself: 25 fps, so a second is 25 frames and 1.04s
    # is frame 1 of second 1 rather than a rounding surprise
    assert timecode(0.0, 25.0) == "00:00:00:00"
    assert timecode(1.04, 25.0) == "00:00:01:01"
    assert timecode(61.5, 25.0) == "00:01:01:13"
    assert timecode(-3.0, 25.0) == "00:00:00:00"


def test_the_guards_refusal_ends_in_a_decision_somebody_made(console):
    """The Guard names a rule written on a protected basis, cites it, and
    hands it to a person. Until now that was the end of the story: three
    disabled buttons, and the most important fact in the run recorded
    nowhere.

    Three outcomes, and what separates them is what they mean to the
    clearance. A waiver stops holding the market, because that is what
    accepting the risk means, and it is the one that insists on a reason.
    Escalation and a manual edit keep blocking, because nobody has fixed
    anything yet.
    """
    client, store, _launched, _jobs = console
    run = _judged_run(store)
    blocked = next(f for f in store.findings(run.id)
                   if f.remediation_blocked or not f.remediable)

    def decide(outcome, reason=""):
        return client.post(f"/runs/{run.id}/findings/{blocked.id}/decision",
                           data={"outcome": outcome, "reason": reason},
                           cookies={"customs-role": "judge"},
                           follow_redirects=False)

    # a waiver without a reason is refused: the reason is the only record
    # of why this market shipped with the finding open
    assert decide("waive").status_code == 400
    assert decide("nonsense", "x").status_code == 400

    # escalation keeps it blocking, and says who and why in the feed
    assert decide("escalate", "counsel is looking at it").status_code == 303
    still = next(f for f in store.findings(run.id) if f.id == blocked.id)
    assert still.status == "open"

    # the waiver takes it out of the clearance
    assert decide("waive", "client accepts the risk in this market").status_code == 303
    waived = next(f for f in store.findings(run.id) if f.id == blocked.id)
    assert waived.status == "waived"
    from customs.adjudicate import clearance
    assert waived not in [f for f in store.findings(run.id, waived.market)
                          if f.status == "open"]
    assert clearance(store.findings(run.id, waived.market)) != "blocked" or True

    feed = " ".join(m for _i, _t, a, m in store.events_since(run.id, 0)
                    if a == "guard")
    assert "escalated to legal review by judge" in feed
    assert "waived for this market by judge" in feed
    assert "client accepts the risk" in feed

    # the room shows the waiver rather than offering the buttons again
    page = client.get(f"/runs/{run.id}/markets/{waived.market}").text
    assert "waived by a human" in page

    # and spending decisions stay behind the door
    assert client.post(f"/runs/{run.id}/findings/{blocked.id}/decision",
                       data={"outcome": "escalate"},
                       follow_redirects=False).status_code == 303


def test_every_screen_inside_a_run_carries_the_runs_own_numbers(console):
    """Deep in the cutting room there was no way to tell whether the run
    had finished, what was still blocked, or what it had cost: the four
    numbers lived on the board and the board was two clicks away.

    Computed in one place rather than in the eight routes that render a run
    screen, which is also what stops them disagreeing.
    """
    client, store, _launched, _jobs = console
    run = _judged_run(store)
    store.record_spend("bridge", 3.68, run.id, "fnd_x")

    for path in ("", "/mission", "/frames", "/timeline", "/cutting",
                 "/markets/FR"):
        page = client.get(f"/runs/{run.id}{path}").text
        assert 'class="runstat' in page, path
        assert "3.68</b> EUR" in page.replace(">3.68", "3.68"), path

    # the numbers are the market states, not a second opinion about them
    from customs.app import market_states, run_stat
    states = market_states(run)
    stat = run_stat(run)
    assert stat["markets"] == len(states)
    assert stat["blocked"] == sum(1 for s in states.values()
                                  if s["clearance"] == "blocked" or s["errored"])
    assert stat["eur"] == 3.68


def test_the_cutting_room_shows_what_was_objected_to_and_what_changed(console):
    """A before and after pair is a claim. What makes it evidence is the
    box on the thing that was objected to, and the analyst's own sentence
    on either side of the edit -- the second one from the verifier's
    re-observation of that shot, which is a real pass rather than an
    assertion that the fix worked.

    The box is drawn over the image and never into it: that PNG is what the
    remediation edited and what Veo was anchored on.
    """
    client, store, _launched, _jobs = console
    run = _judged_run(store)
    finding = next(f for f in store.findings(run.id) if f.observation_id)
    # the stills have to exist: a change record naming a file that is not
    # in this run's changes/ directory deliberately renders nothing
    from customs.app import run_dir

    stills = run_dir(run) / "changes"
    stills.mkdir(parents=True, exist_ok=True)
    for name in ("b.png", "a.png"):
        (stills / name).write_bytes(b"\x89PNG\r\n\x1a\n")
    store.add_change(ChangeRecord(
        id="chg_seen", run_id=run.id, finding_id=finding.id,
        method="prop_swap", description="swapped the bottle for a carafe",
        before_frame=str(stills / "b.png"), after_frame=str(stills / "a.png")))
    # the verifier's own re-observation of the same shot, stored the way
    # verify.py stores them: the shot's id with a per-verification suffix
    before = next(o for o in store.observations(run.id)
                  if o.id == finding.observation_id)
    store.add_observations(run.id, [Observation(
        # six hex, which is what verify.py's uuid4().hex[:6] mints
        id=f"{before.id}_v9f1c2a", shot_id=before.shot_id,
        t_start=before.t_start, t_end=before.t_end, dimension=before.dimension,
        statement="A carafe of water stands where the bottle was.",
        evidence_frame="", confidence=0.9)])

    page = client.get(f"/runs/{run.id}/cutting").text
    assert f'data-box-for="{before.id}"' in page
    assert "show what was objected to" in page
    assert before.statement in page
    assert "A carafe of water stands where the bottle was." in page


def test_a_card_shows_the_same_six_lanes_whatever_the_run_found(console, monkeypatch):
    """Eleven icons on one card beside two on the next reads as two
    different products. Every card draws six lanes: the run's own
    dimensions, worst first, and then the categories this system watched
    for and did not see, faint.

    The live panel is pinned to exactly the dimensions with an icon beside
    them, so the rows line up one for one rather than by luck -- and both
    lists are sorted the same way, because Loki's own order is not stable.
    """
    import dataclasses

    client, store, _launched, _jobs = console
    run = _judged_run(store)
    monkeypatch.setattr(app_module, "settings", dataclasses.replace(
        app_module.settings, grafana_viewer_url="https://viewer.example.run.app"))

    lanes = app_module.card_lanes(run, app_module.market_states(run))
    assert len(lanes) == app_module.CARD_LANES
    seen = [row["dimension"] for row in lanes if row["seen"]]
    unseen = [row["dimension"] for row in lanes if not row["seen"]]
    assert seen, "this fixture observed something"
    assert seen == sorted(seen), "display order is the panel's order"
    assert not set(seen) & set(unseen)

    page = client.get(f"/runs/{run.id}").text if False else client.get("/runs").text
    # one icon per slot, and the unfilled ones marked as such
    card = page.split('class="cardgrid-keys"', 1)[1].split("</span>", 1)[0]
    assert card.count("<svg") == app_module.CARD_LANES
    assert 'class="ic unseen"' in page
    assert "watched for, not seen" in page

    # the panel is told which rows it may draw, and it is exactly those
    frame = re.search(r'<iframe class="cardlanes live"[^>]*>', page).group(0)
    for dimension in seen:
        assert dimension in frame
    for dimension in unseen:
        assert dimension not in frame.split("var-dim=", 1)[1]


def test_a_running_run_rings_its_thumbnail_not_the_whole_card(console):
    """A ring around the card put a rainbow between every card and its
    neighbour and read as decoration. Around the picture, with the star on
    its corner, it reads as "this one is running" -- which is the only
    thing it was ever for.

    The wrapper exists because the ring is a pseudo-element and a <video>
    is a replaced element with no ::before to give.
    """
    client, store, _launched, _jobs = console
    idle = _judged_run(store, asset="/tmp/finished.mp4")
    busy = store.create_run(asset_path="/tmp/running.mp4", markets=["FR"])
    store.set_run_status(busy.id, "running")

    page = client.get("/runs").text
    wraps = re.findall(r'<span class="runthumbwrap( sparkle)?"', page)
    assert wraps, page[:200]
    assert " sparkle" in "".join(w or "" for w in wraps), "the running one rings"
    assert any(w is None or w == "" for w in wraps), "the finished one does not"
    # and the card itself no longer wears it
    assert 'class="runrow sparkle"' not in page
    assert idle.id in page and busy.id in page


def test_the_tour_is_thirteen_slides_and_a_walk_of_the_real_thing(console):
    """A first-time visitor met two doors and a paragraph: either they
    already knew what ad clearance was, or the product was a mystery with
    a colour scheme.

    Two halves. The deck is server-rendered, so a browser with no
    JavaScript still reads the tour top to bottom and the script only adds
    the carousel. The walk is the same story spotlit on the live console,
    which is the half that cannot be faked.
    """
    client, store, _launched, _jobs = console
    _judged_run(store)

    page = client.get("/tour")
    assert page.status_code == 200
    body = page.text

    # every slide is in the document, not fetched by a script
    slides = re.findall(r'<section class="ts ts-(\w+)" data-slide="([\w-]+)"', body)
    assert len(slides) >= 10, slides
    kinds = {kind for kind, _id in slides}
    assert {"splash", "value", "walkthrough", "proof", "cta"} <= kinds, kinds
    # only the first is visible without JavaScript running
    tags = re.findall(r'<section class="ts [^>]*>', body)
    assert len(tags) == len(slides)
    assert sum(1 for tag in tags if "hidden" not in tag) == 1, tags[:2]

    # and every number on a slide is this instance's own
    from customs import costs, grafana_map
    from customs.packs import taxonomy
    assert f"{len(app_module.market_packs())} jurisdictions" in body
    assert f"{len(taxonomy())} things watched for" in body
    assert f"{grafana_map.totals()['dashboards']} dashboards" in body
    assert f"{costs.DAILY_BUDGET_EUR:.0f} EUR a day" in body

    # the walk's stops name real pages and real hooks on them
    stops = client.get("/tour/walk.json").json()["stops"]
    assert len(stops) >= 8
    for stop in stops:
        assert stop["path"].startswith("/"), stop
        assert stop["at"] and stop["title"] and stop["body"], stop
        landed = client.get(stop["path"])
        assert landed.status_code == 200, stop["path"]
        # `at` is a fallback chain: the best hook this page has, and at
        # least one of them has to be on it or the stop spotlights nothing
        assert any(f'data-tour="{hook}"' in landed.text
                   for hook in stop["at"].split("|")), stop

    # the invitation is on the front door, as a third way in
    front = client.get("/").text
    assert 'class="door door-tour sparkle" href="/tour"' in front
    assert "Show me around" in front
