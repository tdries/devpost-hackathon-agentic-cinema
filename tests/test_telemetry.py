import dataclasses
import json

import pytest

from customs import telemetry
from customs.schema import ChangeRecord, Finding, RunRecord
from customs.store import Store

# --- fixtures / helpers ---

def _finding(**overrides):
    # Same rule_id/market/t_start/t_end as tests/test_store.py's fixture --
    # FR-ALC-01 is a real markets/FR.yaml rule (dimension
    # alcohol_tobacco_drugs, klass legal, severity 95), so push_timeline's
    # dimension lookup exercises the real pack join, not a stub.
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

def _run(**overrides):
    fields = dict(
        id="run1", asset_path="docs/samples/test_ad.mp4", t0=1_700_000_000.0,
        status="running", markets=["FR"],
    )
    fields.update(overrides)
    return RunRecord(**fields)

class _FakeResp:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text

@pytest.fixture
def posts(monkeypatch):
    """Capture every telemetry._post call instead of hitting the network.
    Each entry: (url, json_body, headers, auth)."""
    calls = []
    def fake_post(url, *, json_body, headers, auth=None):
        calls.append((url, json_body, headers, auth))
        return _FakeResp(200)
    monkeypatch.setattr(telemetry, "_post", fake_post)
    return calls

def _otlp_bodies(posts_calls):
    """Every OTLP metrics payload posted, in order."""
    return [body for (url, body, _, _) in posts_calls if url.endswith("/v1/metrics")]

def _data_points(otlp_body, metric_name):
    for m in otlp_body["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]:
        if m["name"] == metric_name:
            return m["gauge"]["dataPoints"]
    return None

def _attrs(data_point):
    return {a["key"]: a["value"]["stringValue"] for a in data_point["attributes"]}

# --- push_timeline: sample-per-second correctness ---

def test_push_timeline_finding_span_covers_expected_seconds(monkeypatch, posts, tmp_path):
    monkeypatch.setattr(telemetry.time, "time", lambda: 1_700_000_100.0)
    store = Store(tmp_path / "t.db")
    run = store.create_run(asset_path="docs/samples/test_ad.mp4", markets=["FR"])
    findings = [_finding(run_id=run.id)]  # t_start=12.4, t_end=14.1

    telemetry.push_timeline(run, findings, duration=20.0, store=store)

    bodies = _otlp_bodies(posts)
    assert len(bodies) == 1, "one OTLP payload for the whole call"
    points = _data_points(bodies[0], "customs_risk")
    assert len(points) == 20  # ceil(20.0) seconds, one market

    by_second = {i: p for i, p in enumerate(points)}
    for n in (12, 13, 14):
        attrs = _attrs(by_second[n])
        assert by_second[n]["asDouble"] == 95.0
        assert attrs["dimension"] == "alcohol_tobacco_drugs"
        assert attrs["market"] == "FR"
        assert attrs["asset"] == "test_ad"

    for n in [s for s in range(20) if s not in (12, 13, 14)]:
        attrs = _attrs(by_second[n])
        assert by_second[n]["asDouble"] == 0.0
        assert attrs["dimension"] == "none"

def test_push_timeline_maps_seconds_onto_t0_minus_duration_clock(monkeypatch, posts, tmp_path):
    monkeypatch.setattr(telemetry.time, "time", lambda: 1_700_000_100.0)
    store = Store(tmp_path / "t.db")
    run = store.create_run(asset_path="a.mp4", markets=["FR"])
    findings = [_finding(run_id=run.id, t_start=0.0, t_end=1.0)]

    telemetry.push_timeline(run, findings, duration=5.0, store=store)

    expected_t0 = 1_700_000_100.0 - 5.0
    points = _data_points(_otlp_bodies(posts)[0], "customs_risk")
    for n, p in enumerate(points):
        assert p["timeUnixNano"] == str(int((expected_t0 + n) * 1_000_000_000))

    # t0 is persisted on the run record so later push_log/annotate calls agree
    assert store.get_run(run.id).t0 == expected_t0

def test_push_timeline_covers_every_run_market_even_with_no_findings(monkeypatch, posts, tmp_path):
    # Reviewer-caught bug (fixed post-review): the original implementation
    # derived markets from `findings` alone, so a cleanly-cleared market
    # (in run.markets, zero findings) got no customs_risk series at all --
    # a missing row on the Task 12 heatmap, indistinguishable from a market
    # that was never evaluated. push_timeline must iterate run.markets.
    monkeypatch.setattr(telemetry.time, "time", lambda: 1_700_000_100.0)
    store = Store(tmp_path / "t.db")
    run = store.create_run(asset_path="a.mp4", markets=["FR", "SA", "US"])
    findings = [_finding(run_id=run.id, market="FR")]  # SA, US: clean, no findings

    telemetry.push_timeline(run, findings, duration=3.0, store=store)

    points = _data_points(_otlp_bodies(posts)[0], "customs_risk")
    markets_seen = {_attrs(p)["market"] for p in points}
    assert markets_seen == {"FR", "SA", "US"}  # every run market gets a series

    n_seconds = 3
    for market in ("SA", "US"):
        market_points = [p for p in points if _attrs(p)["market"] == market]
        assert len(market_points) == n_seconds  # full one-per-second coverage
        for p in market_points:
            assert p["asDouble"] == 0.0
            assert _attrs(p)["dimension"] == "none"

def test_push_timeline_all_clean_run_still_pushes(monkeypatch, posts, tmp_path):
    # An all-clean run (zero findings anywhere) must still push -- not
    # silently no-op -- so every market's clean status is visible on the
    # heatmap too, not just via push_status's separate customs_market_status.
    monkeypatch.setattr(telemetry.time, "time", lambda: 1_700_000_100.0)
    store = Store(tmp_path / "t.db")
    run = store.create_run(asset_path="a.mp4", markets=["FR", "SA"])

    telemetry.push_timeline(run, [], duration=2.0, store=store)

    bodies = _otlp_bodies(posts)
    assert len(bodies) == 1
    points = _data_points(bodies[0], "customs_risk")
    assert len(points) == 4  # 2 markets x 2 seconds
    assert all(p["asDouble"] == 0.0 for p in points)
    assert all(_attrs(p)["dimension"] == "none" for p in points)
    assert {_attrs(p)["market"] for p in points} == {"FR", "SA"}

def test_push_timeline_tie_broken_by_input_order(monkeypatch, posts, tmp_path):
    monkeypatch.setattr(telemetry.time, "time", lambda: 1_700_000_100.0)
    store = Store(tmp_path / "t.db")
    run = store.create_run(asset_path="a.mp4", markets=["FR"])
    # both cover second 5, equal severity; first one in the list should win
    first = _finding(run_id=run.id, id="f1", rule_id="FR-ALC-01", t_start=5.0, t_end=6.0, severity=80)
    second = _finding(run_id=run.id, id="f2", rule_id="FR-TOB-01", t_start=5.0, t_end=6.0, severity=80)

    telemetry.push_timeline(run, [first, second], duration=7.0, store=store)

    points = _data_points(_otlp_bodies(posts)[0], "customs_risk")
    attrs = _attrs(points[5])
    assert attrs["dimension"] == "alcohol_tobacco_drugs"  # FR-ALC-01's dimension, not FR-TOB-01's

# --- push_status: 0/1/2 mapping + customs_blocking filtering ---

@pytest.mark.parametrize("clearance,code", [("cleared", 0), ("at_risk", 1), ("blocked", 2)])
def test_push_status_clearance_code_mapping(posts, clearance, code):
    run = _run()
    telemetry.push_status(run, "FR", clearance, findings=[])

    body = _otlp_bodies(posts)[0]
    points = _data_points(body, "customs_market_status")
    assert len(points) == 1
    assert points[0]["asDouble"] == float(code)
    assert _attrs(points[0]) == {"asset": "test_ad", "market": "FR"}

def test_push_status_blocking_only_open_sourced_legal_over_threshold(posts):
    run = _run()
    findings = [
        _finding(id="blocks", market="FR", klass="legal", sourced=True, status="open", severity=95),
        _finding(id="policy_high", market="FR", klass="policy", sourced=True, status="open", severity=95),
        _finding(id="unsourced", market="FR", klass="legal", sourced=False, status="open", severity=95),
        _finding(id="resolved", market="FR", klass="legal", sourced=True, status="resolved", severity=95),
        _finding(id="low_severity", market="FR", klass="legal", sourced=True, status="open", severity=10),
        _finding(id="other_market", market="SA", klass="legal", sourced=True, status="open", severity=95),
    ]

    telemetry.push_status(run, "FR", "blocked", findings=findings)

    body = _otlp_bodies(posts)[0]
    points = _data_points(body, "customs_blocking")
    assert len(points) == 1
    assert points[0]["asDouble"] == 95.0
    assert _attrs(points[0]) == {"asset": "test_ad", "market": "FR", "rule_id": "FR-ALC-01"}

def test_push_status_no_blocking_findings_omits_customs_blocking_metric(posts):
    run = _run()
    telemetry.push_status(run, "FR", "cleared", findings=[])

    body = _otlp_bodies(posts)[0]
    assert _data_points(body, "customs_blocking") is None

def test_push_status_is_current_clock_not_mapped(monkeypatch, posts):
    # run.t0 is far in the past; push_status must ignore it entirely.
    monkeypatch.setattr(telemetry.time, "time", lambda: 1_700_000_500.0)
    run = _run(t0=1.0)
    telemetry.push_status(run, "FR", "cleared", findings=[])
    points = _data_points(_otlp_bodies(posts)[0], "customs_market_status")
    assert points[0]["timeUnixNano"] == str(int(1_700_000_500.0 * 1_000_000_000))

# --- push_log: exact Loki labels ---

def test_push_log_labels_and_body(posts, monkeypatch):
    # Dummy tokens, not the real ones from .env: if this assertion ever
    # fails, pytest's failure diff must never be able to print a real
    # credential.
    monkeypatch.setattr(telemetry, "settings", dataclasses.replace(
        telemetry.settings, loki_user="dummy-loki-user", grafana_cloud_token="dummy-cloud-token",
    ))
    run = _run(t0=1_700_000_000.0)
    finding = _finding()

    telemetry.push_log(run, finding)

    loki_calls = [c for c in posts if c[0] == telemetry.settings.loki_push_url]
    assert len(loki_calls) == 1
    _, body, headers, auth = loki_calls[0]
    stream = body["streams"][0]
    assert stream["stream"] == {
        "app": "customs", "asset": "test_ad", "market": "FR",
        "klass": "legal", "rule_id": "FR-ALC-01",
    }
    [[ts_ns, line]] = stream["values"]
    assert ts_ns == str(int((1_700_000_000.0 + finding.t_start) * 1_000_000_000))
    assert json.loads(line) == finding.to_json()
    assert auth == ("dummy-loki-user", "dummy-cloud-token")

def test_push_log_requires_t0(posts):
    run = _run(t0=None)
    with pytest.raises(ValueError):
        telemetry.push_log(run, _finding())

# --- push_stage_error: current-clock incrementing counter ---

def test_push_stage_error_increments_per_call(monkeypatch, posts):
    monkeypatch.setattr(telemetry, "_stage_error_counts", {})
    monkeypatch.setattr(telemetry.time, "time", lambda: 1_700_000_900.0)
    run = _run(id="run_stage")

    telemetry.push_stage_error(run, "analyst")
    telemetry.push_stage_error(run, "analyst")
    telemetry.push_stage_error(run, "analyst")

    bodies = _otlp_bodies(posts)
    values = [_data_points(b, "customs_stage_error")[0]["asDouble"] for b in bodies]
    assert values == [1.0, 2.0, 3.0]
    attrs = _attrs(_data_points(bodies[-1], "customs_stage_error")[0])
    assert attrs == {"asset": "test_ad", "stage": "analyst"}

def test_push_stage_error_counts_independently_per_stage(monkeypatch, posts):
    monkeypatch.setattr(telemetry, "_stage_error_counts", {})
    run = _run(id="run_stage2")

    telemetry.push_stage_error(run, "analyst")
    telemetry.push_stage_error(run, "ingest")
    telemetry.push_stage_error(run, "analyst")

    bodies = _otlp_bodies(posts)
    values_by_stage = [
        (_attrs(_data_points(b, "customs_stage_error")[0])["stage"],
         _data_points(b, "customs_stage_error")[0]["asDouble"])
        for b in bodies
    ]
    assert values_by_stage == [("analyst", 1.0), ("ingest", 1.0), ("analyst", 2.0)]

# --- annotate / annotate_resolution: payload shape ---

def test_annotate_payload_shape(posts, monkeypatch):
    # Dummy token, not the real one from .env: same reasoning as
    # test_push_log_labels_and_body above -- a failure diff must never be
    # able to print a real credential.
    monkeypatch.setattr(telemetry, "settings", dataclasses.replace(
        telemetry.settings, grafana_sa_token="dummy-sa-token",
    ))
    run = _run(t0=1_700_000_000.0)
    finding = _finding()  # t_start=12.4, t_end=14.1

    telemetry.annotate(run, finding)

    ann_calls = [c for c in posts if c[0].endswith("/api/annotations")]
    assert len(ann_calls) == 1
    _, body, headers, auth = ann_calls[0]
    assert body["time"] == int((1_700_000_000.0 + 12.4) * 1000)
    assert body["timeEnd"] == int((1_700_000_000.0 + 14.1) * 1000)
    assert body["tags"] == ["customs", "test_ad", "FR", "FR-ALC-01"]
    assert "text" in body and isinstance(body["text"], str)
    assert headers["Authorization"] == "Bearer dummy-sa-token"

def test_annotate_resolution_payload_shape_tags_resolved(posts, tmp_path):
    store = Store(tmp_path / "t.db")
    run = store.create_run(asset_path="docs/samples/test_ad.mp4", markets=["FR"])
    store.set_run_t0(run.id, 1_700_000_000.0)
    run = store.get_run(run.id)

    finding = _finding(run_id=run.id)
    store.add_findings([finding])
    change = ChangeRecord(
        id="chg1", run_id=run.id, finding_id=finding.id, method="prop_swap",
        description="replaced wine glass with water glass",
        before_frame="frames/before.jpg", after_frame="frames/after.jpg",
    )

    telemetry.annotate_resolution(run, change, store=store)

    ann_calls = [c for c in posts if c[0].endswith("/api/annotations")]
    assert len(ann_calls) == 1
    _, body, _, _ = ann_calls[0]
    assert body["time"] == int((1_700_000_000.0 + 12.4) * 1000)
    assert body["timeEnd"] == int((1_700_000_000.0 + 14.1) * 1000)
    assert body["tags"] == ["customs", "test_ad", "FR", "FR-ALC-01", "resolved"]

def test_annotate_resolution_unknown_finding_raises(posts, tmp_path):
    store = Store(tmp_path / "t.db")
    run = store.create_run(asset_path="a.mp4", markets=["FR"])
    store.set_run_t0(run.id, 1_700_000_000.0)
    run = store.get_run(run.id)
    change = ChangeRecord(
        id="chg1", run_id=run.id, finding_id="does_not_exist", method="prop_swap",
        description="", before_frame="", after_frame="",
    )

    with pytest.raises(ValueError):
        telemetry.annotate_resolution(run, change, store=store)

# --- push failure surfaces to the caller ---

def test_push_failure_raises(monkeypatch):
    def failing_post(url, *, json_body, headers, auth=None):
        return _FakeResp(500, "internal error")
    monkeypatch.setattr(telemetry, "_post", failing_post)
    run = _run()
    with pytest.raises(RuntimeError):
        telemetry.push_status(run, "FR", "cleared", findings=[])

# --- Step 3: live smoke ---

@pytest.mark.live
def test_push_timeline_live_customs_risk_queryable(tmp_path):
    import httpx

    store = Store(tmp_path / "t.db")
    run = store.create_run(asset_path="telemetry_smoke_test.mp4", markets=["FR", "SA"])
    findings = [
        _finding(
            id="smoke_f1", run_id=run.id, market="FR", rule_id="FR-ALC-01",
            klass="legal", severity=88, t_start=1.0, t_end=2.5,
        ),
        _finding(
            id="smoke_f2", run_id=run.id, market="FR", rule_id="FR-TOB-01",
            klass="legal", severity=60, t_start=4.0, t_end=5.0,
        ),
        _finding(
            id="smoke_f3", run_id=run.id, market="SA", rule_id="SA-MOD-01",
            klass="legal", severity=70, t_start=2.0, t_end=3.0,
        ),
    ]

    telemetry.push_timeline(run, findings, duration=8.0, store=store)

    run = store.get_run(run.id)
    print(f"\nrun.t0 = {run.t0}")

    asset = telemetry._asset_label(run)
    query = f'customs_risk{{asset="{asset}"}}'
    url = telemetry.settings.grafana_url.rstrip("/") + "/api/datasources/proxy/uid/grafanacloud-prom/api/v1/query_range"
    resp = httpx.get(
        url,
        params={
            "query": query,
            "start": f"{run.t0:.3f}",
            "end": f"{run.t0 + 8.0:.3f}",
            "step": "1s",
        },
        headers={"Authorization": f"Bearer {telemetry.settings.grafana_sa_token}"},
        timeout=30.0,
    )
    print(f"query_range status={resp.status_code}")
    data = resp.json()
    result = data.get("data", {}).get("result", [])
    print(f"series returned: {len(result)}")
    for series in result:
        print(f"  {series['metric']} -> {len(series['values'])} point(s)")

    assert resp.status_code == 200
    assert result, f"expected nonempty customs_risk result, got: {data}"
