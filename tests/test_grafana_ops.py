"""Offline tests for grafana_ops: URL shapes, transport fallback, dashboard JSON.

Every test here is hermetic. Two seams, mirroring telemetry.py's single
module-level `_post`:

  * HTTP: monkeypatch `grafana_ops._http`, the one function every REST call
    funnels through.
  * MCP: pass a `_FakeMcp` into `GrafanaOps(..., mcp=...)`, which never
    spawns the real mcp-grafana binary.

`mcp_tools` is always injected explicitly so no test ever depends on what a
live Grafana happens to expose.
"""
import base64
import io
import json
import pathlib

import pytest

from customs import grafana_ops
from customs.grafana_ops import MAPPING, GrafanaOps
from customs.schema import RunRecord

DASHBOARD_DIR = pathlib.Path(grafana_ops.__file__).resolve().parents[2] / "grafana" / "dashboards"

EXPECTED_DASHBOARDS = {
    "customs-overview",
    "customs-timeline",
    "customs-findings",
    "customs-market",
    "customs-remediation",
    "customs-history",
}

PROM_UID = "grafanacloud-prom"
LOKI_UID = "grafanacloud-logs"
# Text and annotation-list panels have no data source of their own. They carry
# Grafana's built-in "-- Grafana --" datasource explicitly rather than being
# left null, so that no panel in any of the six files inherits the org default
# (which is what the "explicit datasource" rule is actually protecting against).
BUILTIN_UID = "grafana"


# --- fixtures / helpers ---

class _Settings:
    """Just the fields grafana_ops reads. Deliberately not customs.config's
    real Settings: these tests must not depend on a .env being present."""
    grafana_url = "https://example.grafana.net"
    grafana_sa_token = "fake-token-not-a-real-secret"


class _FakeResp:
    def __init__(self, status_code=200, payload=None, content=b"", text=None):
        self.status_code = status_code
        self._payload = payload
        self.content = content
        self.text = text if text is not None else json.dumps(payload or {})

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class _FakeMcp:
    """Stands in for _McpStdio. Records (tool, arguments) and replays canned
    results keyed by tool name. A canned result may be a callable taking the
    arguments dict, for tools whose answer depends on the operation asked for
    (alerting_manage_rules get-then-create, for instance)."""

    def __init__(self, tools=(), results=None):
        self.tools = set(tools)
        self.results = results or {}
        self.calls = []
        self.closed = False

    def list_tools(self):
        return set(self.tools)

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        canned = self.results.get(name, {"content": [{"type": "text", "text": "{}"}]})
        return canned(arguments) if callable(canned) else canned

    def close(self):
        self.closed = True


def _run(**overrides):
    fields = dict(
        id="run1", asset_path="docs/samples/test_ad.mp4", t0=1_700_000_000.0,
        status="running", markets=["FR", "SA", "US"],
    )
    fields.update(overrides)
    return RunRecord(**fields)


def _ops(mcp_tools=(), mcp=None, **kw):
    return GrafanaOps(_Settings(), mcp=mcp, mcp_tools=set(mcp_tools), **kw)


class _Calls(list):
    """A list of captured HTTP calls that also carries the response queue."""
    queue: list


@pytest.fixture
def http(monkeypatch):
    """Capture every grafana_ops._http call. Each entry is a dict with
    method/url/params/json/headers. Responses are supplied by pushing onto
    `queue`; anything unqueued gets a bare 200 {}."""
    calls = _Calls()
    queue = []

    def fake_http(method, url, *, headers, json_body=None, params=None, timeout=60.0):
        calls.append({
            "method": method, "url": url, "headers": headers,
            "json": json_body, "params": params,
        })
        if queue:
            return queue.pop(0)
        return _FakeResp(200, {})

    monkeypatch.setattr(grafana_ops, "_http", fake_http)
    calls.queue = queue  # type: ignore[attr-defined]
    return calls


# --- transport fallback decision (fake tool inventory) ---

def test_every_contract_op_is_in_the_mapping():
    for op in ("ensure_dashboards", "ensure_alert_rules", "ensure_contact_point",
               "enable_public", "render_png", "query_history"):
        assert op in MAPPING


def test_transport_is_http_for_every_op_when_inventory_is_empty():
    ops = _ops(mcp_tools=())
    for op in MAPPING:
        assert ops.transport_for(op) == "http"


def test_transport_is_mcp_when_the_matching_write_tool_is_present():
    ops = _ops(mcp_tools={"update_dashboard", "alerting_manage_rules",
                          "get_panel_image", "query_loki_logs", "create_folder"})
    assert ops.transport_for("ensure_dashboards") == "mcp"
    assert ops.transport_for("ensure_alert_rules") == "mcp"
    assert ops.transport_for("render_png") == "mcp"
    assert ops.transport_for("query_history") == "mcp"


def test_transport_is_http_for_an_op_whose_own_tool_is_missing():
    # A rich inventory that happens not to include update_dashboard must not
    # drag ensure_dashboards onto MCP.
    ops = _ops(mcp_tools={"alerting_manage_rules", "query_loki_logs"})
    assert ops.transport_for("ensure_dashboards") == "http"
    assert ops.transport_for("ensure_alert_rules") == "mcp"


def test_contact_point_and_public_are_http_only_whatever_the_inventory_says():
    # mcp-grafana 1.1.0 has no write tool for either (alerting_manage_routing
    # is read-only, and there is no public-dashboard tool at all), so their
    # MAPPING entries name no tool and can never flip to MCP.
    ops = _ops(mcp_tools={"alerting_manage_routing", "everything", "update_dashboard"})
    assert ops.transport_for("ensure_contact_point") == "http"
    assert ops.transport_for("enable_public") == "http"
    assert MAPPING["ensure_contact_point"].mcp_tool is None
    assert MAPPING["enable_public"].mcp_tool is None


def test_unknown_op_raises():
    with pytest.raises(KeyError):
        _ops().transport_for("ensure_world_peace")


def test_missing_binary_leaves_an_empty_inventory_and_does_not_raise():
    ops = GrafanaOps(_Settings(), mcp_binary="/nonexistent/mcp-grafana")
    assert ops.mcp_tools == set()
    assert ops.transport_for("ensure_dashboards") == "http"


# --- mcp-grafana binary resolution (_default_mcp_binary), task 17 ---
#
# The Cloud Run image has no repo, just /app, so bin/mcp-grafana (a
# gitignored dev binary) is never there; the Dockerfile installs the
# linux/amd64 release binary at /usr/local/bin/mcp-grafana instead.
# _default_mcp_binary is what makes GrafanaOps() with no explicit
# mcp_binary find that binary in the deployed image while still finding
# bin/mcp-grafana on a developer's checkout.

def test_default_mcp_binary_prefers_the_env_var_over_everything(monkeypatch):
    monkeypatch.setenv("MCP_GRAFANA_BIN", "/from/env/mcp-grafana")
    # Even a repo-relative dev binary that exists must lose to the env var.
    monkeypatch.setattr(grafana_ops, "MCP_BINARY", pathlib.Path(__file__))
    assert grafana_ops._default_mcp_binary() == pathlib.Path("/from/env/mcp-grafana")


def test_default_mcp_binary_uses_the_repo_relative_dev_binary_when_present(monkeypatch, tmp_path):
    monkeypatch.delenv("MCP_GRAFANA_BIN", raising=False)
    dev_binary = tmp_path / "mcp-grafana"
    dev_binary.write_text("fake binary")
    monkeypatch.setattr(grafana_ops, "MCP_BINARY", dev_binary)
    assert grafana_ops._default_mcp_binary() == dev_binary


def test_default_mcp_binary_falls_back_to_the_container_path_when_dev_binary_is_absent(monkeypatch, tmp_path):
    monkeypatch.delenv("MCP_GRAFANA_BIN", raising=False)
    monkeypatch.setattr(grafana_ops, "MCP_BINARY", tmp_path / "nonexistent" / "mcp-grafana")
    assert grafana_ops._default_mcp_binary() == grafana_ops._CONTAINER_MCP_BINARY
    assert grafana_ops._CONTAINER_MCP_BINARY == pathlib.Path("/usr/local/bin/mcp-grafana")


def test_grafana_ops_with_no_binary_kwarg_goes_through_default_resolution(monkeypatch, tmp_path):
    """GrafanaOps() with no mcp_binary must resolve through
    _default_mcp_binary, not silently ignore MCP_GRAFANA_BIN / the
    container fallback the way the old `mcp_binary or MCP_BINARY` did."""
    monkeypatch.delenv("MCP_GRAFANA_BIN", raising=False)
    monkeypatch.setattr(grafana_ops, "MCP_BINARY", tmp_path / "no-dev-binary")
    monkeypatch.setattr(grafana_ops, "_CONTAINER_MCP_BINARY", tmp_path / "no-container-binary")
    ops = GrafanaOps(_Settings())
    assert ops.mcp_tools == set()
    assert ops.mcp_error and str(tmp_path / "no-container-binary") in ops.mcp_error


def test_explicit_mcp_binary_kwarg_still_wins_over_the_env_var(monkeypatch):
    """Existing callers (this test file, provision_grafana.py) pass
    mcp_binary= explicitly; the new default-resolution chain must never
    second-guess that."""
    monkeypatch.setenv("MCP_GRAFANA_BIN", "/from/env/mcp-grafana")
    ops = GrafanaOps(_Settings(), mcp_binary="/nonexistent/explicit-path")
    assert ops.mcp_error and "/nonexistent/explicit-path" in ops.mcp_error


# --- embed_url: exact URL shape ---

def test_embed_url_public_shape_is_exact():
    ops = _ops()
    ops.public_tokens["customs-timeline"] = "abc123token"
    url = ops.embed_url("customs-timeline", 2, _run(), duration=30.0)
    assert url == (
        "https://example.grafana.net/public-dashboards/abc123token"
        "?from=1700000000000&to=1700000030000&viewPanel=2"
    )


def test_embed_url_falls_back_to_d_solo_without_a_public_token():
    ops = _ops()
    url = ops.embed_url("customs-timeline", 2, _run(), duration=30.0)
    assert url == (
        "https://example.grafana.net/d-solo/customs-timeline"
        "?panelId=2&from=1700000000000&to=1700000030000"
    )


def test_embed_url_time_range_is_t0_to_t0_plus_duration_in_millis():
    ops = _ops()
    url = ops.embed_url("customs-overview", 1, _run(t0=1_787_000_000.5), duration=42.25)
    assert "from=1787000000500" in url
    assert "to=1787000042750" in url


def test_embed_url_explicit_token_beats_the_cache():
    ops = _ops()
    ops.public_tokens["customs-overview"] = "cached"
    url = ops.embed_url("customs-overview", 1, _run(), duration=1.0, public_token="explicit")
    assert "/public-dashboards/explicit?" in url


def test_embed_url_reads_duration_off_the_run_record_when_not_given():
    # RunRecord has no duration field today; a future one is picked up for
    # free, and until then the documented DEFAULT_EMBED_DURATION_S applies.
    class _RunWithDuration(RunRecord):
        pass

    run = _RunWithDuration(**_run().to_json())
    run.duration = 12.0  # type: ignore[attr-defined]
    url = _ops().embed_url("customs-timeline", 1, run)
    assert "to=1700000012000" in url


def test_embed_url_default_duration_when_nothing_supplies_one():
    url = _ops().embed_url("customs-timeline", 1, _run())
    expected_to = int((1_700_000_000.0 + grafana_ops.DEFAULT_EMBED_DURATION_S) * 1000)
    assert f"to={expected_to}" in url


def test_embed_url_requires_a_mapped_clock():
    with pytest.raises(ValueError, match="t0"):
        _ops().embed_url("customs-timeline", 1, _run(t0=None), duration=10.0)


def test_embed_url_never_touches_the_network(http):
    ops = _ops()
    ops.public_tokens["customs-overview"] = "tok"
    ops.embed_url("customs-overview", 1, _run(), duration=5.0)
    assert http == []


# --- ensure_dashboards ---

def test_ensure_dashboards_uses_mcp_when_update_dashboard_exists(tmp_path, http):
    (tmp_path / "overview.json").write_text(json.dumps(
        {"uid": "customs-overview", "title": "Customs: Clearance Overview", "panels": []}
    ))
    mcp = _FakeMcp(tools={"update_dashboard", "create_folder"})
    ops = _ops(mcp_tools={"update_dashboard", "create_folder"}, mcp=mcp,
               dashboards_dir=tmp_path)
    result = ops.ensure_dashboards()

    assert result == {"overview": "customs-overview"}
    tools_called = [name for name, _ in mcp.calls]
    assert "update_dashboard" in tools_called
    args = dict(mcp.calls[-1][1])
    assert args["dashboard"]["uid"] == "customs-overview"
    assert args["overwrite"] is True
    assert args["folderUid"] == ops.folder_uid
    # nothing went over REST for the dashboard itself
    assert not [c for c in http if "/api/dashboards/db" in c["url"]]


def test_ensure_dashboards_falls_back_to_http(tmp_path, http):
    (tmp_path / "timeline.json").write_text(json.dumps(
        {"uid": "customs-timeline", "title": "Customs: Timeline", "panels": []}
    ))
    ops = _ops(mcp_tools=(), dashboards_dir=tmp_path)
    result = ops.ensure_dashboards()

    assert result == {"timeline": "customs-timeline"}
    posts = [c for c in http if c["url"].endswith("/api/dashboards/db")]
    assert len(posts) == 1
    assert posts[0]["method"] == "POST"
    assert posts[0]["json"]["dashboard"]["uid"] == "customs-timeline"
    assert posts[0]["json"]["overwrite"] is True
    assert posts[0]["headers"]["Authorization"].startswith("Bearer ")


def test_ensure_dashboards_creates_the_folder_first(tmp_path, http):
    (tmp_path / "overview.json").write_text(json.dumps(
        {"uid": "customs-overview", "title": "t", "panels": []}
    ))
    ops = _ops(mcp_tools=(), dashboards_dir=tmp_path)
    ops.ensure_dashboards()
    urls = [c["url"] for c in http]
    assert urls.index("https://example.grafana.net/api/folders") < \
        urls.index("https://example.grafana.net/api/dashboards/db")


# --- ensure_alert_rules ---

def test_ensure_alert_rules_pins_the_two_rules_and_their_expressions():
    titles = [r["title"] for r in grafana_ops.ALERT_RULES]
    assert titles == ["customs_blocking_finding", "customs_market_at_risk"]
    by_title = {r["title"]: r for r in grafana_ops.ALERT_RULES}
    blocking = by_title["customs_blocking_finding"]
    assert blocking["expr"] == "max by (asset, market, rule_id) (customs_blocking) >= 70"
    assert blocking["for"] == "0s"
    assert blocking["labels"]["team"] == "customs"
    at_risk = by_title["customs_market_at_risk"]
    assert at_risk["expr"] == "max by (asset, market) (customs_market_status) >= 1"
    assert at_risk["for"] == "0s"
    assert at_risk["labels"]["team"] == "customs"
    for rule in grafana_ops.ALERT_RULES:
        summary = rule["annotations"]["summary"]
        for label in ("asset", "market", "rule_id"):
            if label in rule["group_by"]:
                assert f"$labels.{label}" in summary


def test_ensure_alert_rules_via_http_creates_then_sets_the_group_interval(http):
    http.queue.extend([
        _FakeResp(200, {"uid": "customs"}),                # POST folder
        _FakeResp(404, {"message": "not found"}),          # GET rule 1
        _FakeResp(202, {"uid": "customs-blocking-finding"}),  # POST rule 1
        _FakeResp(404, {"message": "not found"}),          # GET rule 2
        _FakeResp(202, {"uid": "customs-market-at-risk"}),   # POST rule 2
        _FakeResp(200, {"title": "customs", "folderUid": "customs",
                        "interval": 60, "rules": [{"a": 1}]}),  # GET group
        _FakeResp(200, {}),                                 # PUT group
    ])
    ops = _ops(mcp_tools=())
    ops.ensure_alert_rules()

    posts = [c for c in http if c["method"] == "POST" and c["url"].endswith("/alert-rules")]
    assert len(posts) == 2
    assert posts[0]["json"]["title"] == "customs_blocking_finding"
    assert posts[0]["json"]["labels"]["team"] == "customs"
    assert posts[0]["json"]["for"] == "0s"
    # the PromQL threshold lives in the query itself
    exprs = [q["model"].get("expr") for q in posts[0]["json"]["data"] if "expr" in q["model"]]
    assert exprs == ["max by (asset, market, rule_id) (customs_blocking) >= 70"]

    group_puts = [c for c in http if c["method"] == "PUT" and "rule-groups" in c["url"]]
    assert len(group_puts) == 1
    assert group_puts[0]["json"]["interval"] == 30
    # the PUT must carry the group's existing rules back, or it wipes them
    assert group_puts[0]["json"]["rules"] == [{"a": 1}]


def test_ensure_alert_rules_updates_in_place_when_the_rule_already_exists(http):
    http.queue.extend([
        _FakeResp(200, {"uid": "customs"}),                # POST folder
        _FakeResp(200, {"uid": "customs-blocking-finding", "title": "customs_blocking_finding"}),
        _FakeResp(200, {}),                                # PUT rule 1
        _FakeResp(200, {"uid": "customs-market-at-risk"}),
        _FakeResp(200, {}),                                # PUT rule 2
        _FakeResp(200, {"title": "customs", "interval": 30, "rules": []}),
        _FakeResp(200, {}),
    ])
    ops = _ops(mcp_tools=())
    ops.ensure_alert_rules()
    assert not [c for c in http if c["method"] == "POST" and c["url"].endswith("/alert-rules")]
    puts = [c for c in http if c["method"] == "PUT" and "/alert-rules/" in c["url"]]
    assert len(puts) == 2


def test_ensure_alert_rules_via_mcp_calls_the_alerting_tool(http):
    def rules_tool(args):
        # nothing exists yet: the 'get' probe fails, everything else succeeds
        if args["operation"] == "get":
            return {"content": [{"type": "text", "text": "rule not found"}], "isError": True}
        return {"content": [{"type": "text", "text": json.dumps({"uid": args["rule_uid"]})}]}

    mcp = _FakeMcp(tools={"alerting_manage_rules"}, results={"alerting_manage_rules": rules_tool})
    http.queue.extend([
        _FakeResp(200, {"uid": "customs"}),                # POST folder
        _FakeResp(200, {"title": "customs", "interval": 60, "rules": []}),
        _FakeResp(200, {}),
    ])
    ops = _ops(mcp_tools={"alerting_manage_rules"}, mcp=mcp)
    ops.ensure_alert_rules()

    creates = [a for name, a in mcp.calls if a.get("operation") == "create"]
    assert len(creates) == 2
    assert creates[0]["title"] == "customs_blocking_finding"
    assert creates[0]["rule_group"] == "customs"
    assert creates[0]["for"] == "0s"
    assert creates[0]["labels"]["team"] == "customs"
    # group interval still has to go over REST: no MCP tool sets it
    assert [c["method"] for c in http if "rule-groups" in c["url"]] == ["GET", "PUT"]


# --- ensure_contact_point + notification policy ---

def test_ensure_contact_point_creates_and_routes(http):
    http.queue.extend([
        _FakeResp(200, []),                                  # GET contact-points
        _FakeResp(202, {"uid": "cp1", "name": "customs-webhook"}),  # POST
        _FakeResp(200, {"receiver": "empty", "group_by": ["grafana_folder", "alertname"]}),
        _FakeResp(202, {}),                                  # PUT policies
    ])
    ops = _ops(mcp_tools=())
    ops.ensure_contact_point("https://example.invalid/hooks/customs")

    post = [c for c in http if c["method"] == "POST" and c["url"].endswith("/contact-points")][0]
    assert post["json"]["name"] == "customs-webhook"
    assert post["json"]["type"] == "webhook"
    assert post["json"]["settings"]["url"] == "https://example.invalid/hooks/customs"
    assert post["headers"]["X-Disable-Provenance"] == "true"

    put = [c for c in http if c["method"] == "PUT" and c["url"].endswith("/policies")][0]
    child = put["json"]["routes"][0]
    assert child["receiver"] == "customs-webhook"
    assert ["team", "=", "customs"] in child["object_matchers"]
    # the pre-existing tree is preserved, not replaced
    assert put["json"]["receiver"] == "empty"


def test_ensure_contact_point_updates_an_existing_one_by_name(http):
    http.queue.extend([
        _FakeResp(200, [{"uid": "cp1", "name": "customs-webhook", "type": "webhook"}]),
        _FakeResp(202, {}),                                  # PUT contact-point
        _FakeResp(200, {"receiver": "empty"}),
        _FakeResp(202, {}),
    ])
    ops = _ops(mcp_tools=())
    ops.ensure_contact_point("https://example.invalid/hooks/v2")
    puts = [c for c in http if c["method"] == "PUT" and "/contact-points/cp1" in c["url"]]
    assert len(puts) == 1
    assert puts[0]["json"]["settings"]["url"] == "https://example.invalid/hooks/v2"


def test_notification_policy_is_not_duplicated_on_reprovision(http):
    existing = {
        "receiver": "empty",
        "routes": [{"receiver": "customs-webhook",
                    "object_matchers": [["team", "=", "customs"]]}],
    }
    http.queue.extend([
        _FakeResp(200, [{"uid": "cp1", "name": "customs-webhook"}]),
        _FakeResp(202, {}),
        _FakeResp(200, existing),
        _FakeResp(202, {}),
    ])
    ops = _ops(mcp_tools=())
    ops.ensure_contact_point("https://example.invalid/hooks/customs")
    put = [c for c in http if c["method"] == "PUT" and c["url"].endswith("/policies")][0]
    customs_routes = [r for r in put["json"]["routes"] if r["receiver"] == "customs-webhook"]
    assert len(customs_routes) == 1


# --- enable_public ---

def test_enable_public_returns_the_public_url_and_caches_the_token(http):
    http.queue.append(_FakeResp(200, {"uid": "pd1", "accessToken": "tok42", "isEnabled": True}))
    ops = _ops(mcp_tools=())
    url = ops.enable_public("customs-overview")
    assert url == "https://example.grafana.net/public-dashboards/tok42"
    assert ops.public_tokens["customs-overview"] == "tok42"
    post = http[0]
    assert post["method"] == "POST"
    assert post["url"].endswith("/api/dashboards/uid/customs-overview/public-dashboards")
    assert post["json"]["isEnabled"] is True


def test_enable_public_patches_an_already_public_dashboard(http):
    http.queue.extend([
        _FakeResp(400, {"message": "public dashboard already exists"}),
        _FakeResp(200, {"uid": "pd1", "accessToken": "existing"}),   # GET
        _FakeResp(200, {"uid": "pd1", "accessToken": "existing", "isEnabled": True}),  # PATCH
    ])
    ops = _ops(mcp_tools=())
    url = ops.enable_public("customs-timeline")
    assert url.endswith("/public-dashboards/existing")
    assert [c["method"] for c in http] == ["POST", "GET", "PATCH"]


# --- render_png ---

def test_render_png_via_mcp_decodes_base64_image_content():
    png = b"\x89PNG\r\n\x1a\nfake"
    mcp = _FakeMcp(tools={"get_panel_image"}, results={
        "get_panel_image": {"content": [
            {"type": "image", "data": base64.b64encode(png).decode(), "mimeType": "image/png"}
        ]},
    })
    ops = _ops(mcp_tools={"get_panel_image"}, mcp=mcp)
    out = ops.render_png("customs-overview", 1, _run(), duration=30.0)
    assert out == png
    args = mcp.calls[0][1]
    assert args["dashboardUid"] == "customs-overview"
    assert args["panelId"] == 1
    # epoch millis, not RFC3339: see the note in render_png
    assert args["timeRange"] == {"from": "1700000000000", "to": "1700000030000"}


def test_render_png_via_http_hits_the_renderer_with_the_mapped_window(http):
    http.queue.append(_FakeResp(200, content=b"\x89PNG\r\n\x1a\nhttp"))
    ops = _ops(mcp_tools=())
    out = ops.render_png("customs-timeline", 2, _run(), duration=30.0)
    assert out.startswith(b"\x89PNG")
    call = http[0]
    assert "/render/d-solo/customs-timeline" in call["url"]
    assert call["params"]["panelId"] == 2
    assert call["params"]["from"] == 1_700_000_000_000
    assert call["params"]["to"] == 1_700_000_030_000


def test_render_png_whole_dashboard_when_panel_id_is_none(http):
    http.queue.append(_FakeResp(200, content=b"\x89PNG"))
    ops = _ops(mcp_tools=())
    ops.render_png("customs-overview", None, _run(), duration=10.0)
    assert "/render/d/customs-overview" in http[0]["url"]
    assert "panelId" not in http[0]["params"]
    # without kiosk a whole dashboard render carries Grafana's nav chrome
    assert http[0]["params"]["kiosk"] == "true"


# --- query_history ---

def test_query_history_builds_the_logql_and_parses_finding_json():
    line = json.dumps({"market": "FR", "rule_id": "FR-ALC-01", "severity": 95})
    mcp = _FakeMcp(tools={"query_loki_logs"}, results={
        "query_loki_logs": {"content": [{"type": "text", "text": json.dumps([
            {"timestamp": "2026-08-01T10:00:00Z",
             "labels": {"asset": "aperolo_summer", "market": "FR", "rule_id": "FR-ALC-01"},
             "line": line},
        ])}]},
    })
    ops = _ops(mcp_tools={"query_loki_logs"}, mcp=mcp)
    rows = ops.query_history("aperolo", "FR-ALC-01")

    args = mcp.calls[0][1]
    assert args["datasourceUid"] == LOKI_UID
    assert args["logql"] == '{app="customs", asset=~".*aperolo.*", rule_id="FR-ALC-01"}'
    assert len(rows) == 1
    assert rows[0]["finding"]["severity"] == 95
    assert rows[0]["labels"]["asset"] == "aperolo_summer"


def test_query_history_via_http_proxy(http):
    line = json.dumps({"market": "SA", "rule_id": "SA-MOD-01", "severity": 80})
    http.queue.append(_FakeResp(200, {"status": "success", "data": {
        "resultType": "streams",
        "result": [{"stream": {"asset": "test_ad", "market": "SA", "rule_id": "SA-MOD-01"},
                    "values": [["1787000000000000000", line]]}],
    }}))
    ops = _ops(mcp_tools=())
    rows = ops.query_history("test", "SA-MOD-01")
    assert "grafanacloud-logs" in http[0]["url"]
    assert http[0]["params"]["query"] == '{app="customs", asset=~".*test.*", rule_id="SA-MOD-01"}'
    assert rows[0]["finding"]["severity"] == 80
    assert rows[0]["labels"]["market"] == "SA"


def test_query_history_escapes_regex_metacharacters_in_the_brand():
    mcp = _FakeMcp(tools={"query_loki_logs"}, results={
        "query_loki_logs": {"content": [{"type": "text", "text": "[]"}]},
    })
    ops = _ops(mcp_tools={"query_loki_logs"}, mcp=mcp)
    ops.query_history("a.b+c", "FR-ALC-01")
    assert r'asset=~".*a\.b\+c.*"' in mcp.calls[0][1]["logql"]


def test_query_history_normalizes_both_transports_timestamps():
    # mcp-grafana returns the Loki timestamp as a JSON-quoted nanosecond string
    line = json.dumps({"rule_id": "FR-ALC-01", "severity": 95})
    mcp = _FakeMcp(tools={"query_loki_logs"}, results={
        "query_loki_logs": {"content": [{"type": "text", "text": json.dumps({
            "data": [{"timestamp": '"1700000000000000000"', "labels": {"asset": "a"},
                      "line": line}],
        })}]},
    })
    rows = _ops(mcp_tools={"query_loki_logs"}, mcp=mcp).query_history("a", "FR-ALC-01")
    assert rows[0]["ts_ns"] == "1700000000000000000"
    assert rows[0]["ts"] == "2023-11-14T22:13:20Z"


def test_query_history_keeps_unparseable_lines_instead_of_dropping_them():
    mcp = _FakeMcp(tools={"query_loki_logs"}, results={
        "query_loki_logs": {"content": [{"type": "text", "text": json.dumps([
            {"timestamp": "2026-08-01T10:00:00Z", "labels": {"asset": "a"},
             "line": "not json at all"},
        ])}]},
    })
    ops = _ops(mcp_tools={"query_loki_logs"}, mcp=mcp)
    rows = ops.query_history("a", "FR-ALC-01")
    assert rows[0]["finding"] is None
    assert rows[0]["line"] == "not json at all"


# --- the six dashboard JSON files ---

def _dashboards():
    return {p.stem: json.loads(p.read_text()) for p in sorted(DASHBOARD_DIR.glob("*.json"))}


def _panels(dash):
    """Every panel, including panels nested inside a row."""
    out = []
    for panel in dash.get("panels", []):
        out.append(panel)
        out.extend(panel.get("panels", []))
    return out


def _strings(obj, path="$"):
    if isinstance(obj, str):
        yield path, obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from _strings(v, f"{path}.{k}")
            yield f"{path}.<key>", k
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _strings(v, f"{path}[{i}]")


def test_all_six_dashboards_exist_with_stable_uids():
    dashboards = _dashboards()
    assert len(dashboards) == 6
    assert {d["uid"] for d in dashboards.values()} == EXPECTED_DASHBOARDS
    for stem, dash in dashboards.items():
        assert dash["uid"] == f"customs-{stem}", stem
        assert dash["title"]
        assert dash["schemaVersion"] >= 39


def test_every_panel_carries_an_explicit_datasource():
    for stem, dash in _dashboards().items():
        panels = _panels(dash)
        assert panels, f"{stem} has no panels"
        for panel in panels:
            ds = panel.get("datasource")
            assert isinstance(ds, dict), f"{stem}/{panel.get('title')} has no datasource"
            assert ds.get("uid") in {PROM_UID, LOKI_UID, BUILTIN_UID}, (stem, ds)


def test_every_query_target_carries_the_verified_datasource_uid():
    for stem, dash in _dashboards().items():
        for panel in _panels(dash):
            for target in panel.get("targets", []):
                ds = target.get("datasource")
                assert isinstance(ds, dict), (stem, panel.get("title"))
                assert ds == panel["datasource"], (stem, panel.get("title"))
                if ds["uid"] == PROM_UID:
                    assert ds["type"] == "prometheus"
                    assert target.get("expr"), (stem, panel.get("title"))
                elif ds["uid"] == LOKI_UID:
                    assert ds["type"] == "loki"
                    assert target.get("expr"), (stem, panel.get("title"))


def test_no_em_dashes_anywhere_in_the_dashboard_json():
    for stem, dash in _dashboards().items():
        for path, value in _strings(dash):
            assert "—" not in value, f"em dash in {stem} at {path}"
            assert "–" not in value, f"en dash in {stem} at {path}"


def test_panel_ids_are_unique_and_stable_within_each_dashboard():
    for stem, dash in _dashboards().items():
        ids = [p["id"] for p in _panels(dash)]
        assert all(isinstance(i, int) for i in ids), stem
        assert len(ids) == len(set(ids)), stem


def test_timeline_has_the_asset_variable_and_a_state_timeline_of_risk_by_market():
    dash = _dashboards()["timeline"]
    variables = {v["name"]: v for v in dash["templating"]["list"]}
    assert "asset" in variables
    assert variables["asset"]["query"]["query"] == "label_values(customs_risk, asset)"
    assert variables["asset"]["datasource"]["uid"] == PROM_UID

    panels = _panels(dash)
    assert panels[0]["type"] in {"state-timeline", "heatmap"}
    expr = panels[0]["targets"][0]["expr"]
    assert "customs_risk" in expr and "by (market)" in expr
    assert "$asset" in expr
    # second panel breaks the same series down by dimension
    assert any("by (dimension)" in t.get("expr", "")
               for p in panels[1:] for t in p.get("targets", []))


def test_market_dashboard_has_the_market_variable_and_the_class_semantics_text():
    dash = _dashboards()["market"]
    variables = {v["name"]: v for v in dash["templating"]["list"]}
    assert "market" in variables
    assert "label_values" in variables["market"]["query"]["query"]
    text_panels = [p for p in _panels(dash) if p["type"] == "text"]
    assert text_panels, "market detail needs the class semantics text panel"
    content = " ".join(p["options"]["content"] for p in text_panels).lower()
    for klass in ("legal", "policy", "offence"):
        assert klass in content
    assert "guard" in content


def test_overview_uses_the_status_metric_with_the_three_clearance_mappings():
    dash = _dashboards()["overview"]
    panels = _panels(dash)
    stat = next(p for p in panels if p["type"] == "stat"
                and "customs_market_status" in p["targets"][0]["expr"])
    mappings = stat["fieldConfig"]["defaults"]["mappings"][0]["options"]
    assert mappings["0"]["text"] == "CLEARED"
    assert mappings["1"]["text"] == "AT RISK"
    assert mappings["2"]["text"] == "BLOCKED"
    steps = stat["fieldConfig"]["defaults"]["thresholds"]["steps"]
    # exact brand hex, not Grafana's named colours: the dashboards use the
    # same four codes as the console's icons.
    assert [s["color"] for s in steps] == ["#34A853", "#FBBC05", "#EA4335"]
    assert [s["value"] for s in steps] == [None, 1, 2]

    assert any(p["type"] == "bargauge" and "customs_blocking" in p["targets"][0]["expr"]
               for p in panels)
    stage = next(p for p in panels if "customs_stage_error" in p["targets"][0].get("expr", ""))
    assert stage["title"] == (
        "Stage errors (a clearance tool that silently skips a shot is worse "
        "than one that admits it)"
    )


def test_findings_dashboard_has_a_logs_panel_and_a_parsed_table():
    dash = _dashboards()["findings"]
    panels = _panels(dash)
    assert any(p["type"] == "logs" for p in panels)
    table = next(p for p in panels if p["type"] == "table")
    expr = table["targets"][0]["expr"]
    # market/rule_id/klass are already stream labels, so `| json` is asked only
    # for the body fields; naming an extracted label after an existing stream
    # label is what makes Loki suffix it with _extracted.
    assert "| json" in expr
    assert 'citation="citation_ref"' in expr
    assert 'severity="severity"' in expr
    assert 'sourced="sourced"' in expr
    ids = [t["id"] for t in table["transformations"]]
    assert ids[0] == "extractFields"
    assert {"convertFieldType", "filterFieldsByName", "organize"} <= set(ids)
    by_id = {t["id"]: t["options"] for t in table["transformations"]}
    # a whitelist of columns, so Loki's own extras (traceID, detected_level)
    # cannot creep into the findings table
    include = by_id["filterFieldsByName"]["include"]["names"]
    for column in ("market", "rule_id", "severity", "sourced", "citation"):
        assert column in include, column
    assert set(by_id["organize"]["indexByName"]) == set(include)
    # severity has to be numeric or it sorts and colours as text
    assert by_id["convertFieldType"]["conversions"][0] == {
        "targetField": "severity", "destinationType": "number"}


def test_remediation_dashboard_lists_customs_annotations():
    dash = _dashboards()["remediation"]
    panels = _panels(dash)
    annolist = next(p for p in panels if p["type"] == "annolist")
    assert annolist["options"]["tags"] == ["customs"]
    assert any(p["type"] == "text" for p in panels)


def test_history_dashboard_groups_by_asset_over_thirty_days():
    dash = _dashboards()["history"]
    assert dash["time"]["from"] == "now-30d"
    exprs = [t["expr"] for p in _panels(dash) for t in p.get("targets", [])]
    assert any("by (asset)" in e for e in exprs)
    assert all(p["datasource"]["uid"] in {LOKI_UID, BUILTIN_UID} for p in _panels(dash))


def test_timeline_and_market_show_customs_annotations():
    # Grafana keeps a tag-based annotation query's tags under `target`, which is
    # where telemetry.annotate's ["customs", asset, market, rule_id] tags get
    # matched.
    for stem in ("timeline", "market", "remediation"):
        dash = _dashboards()[stem]
        queries = dash["annotations"]["list"]
        assert any("customs" in q["target"]["tags"] for q in queries), stem
        # the built-in annotation datasource, named the way Grafana names it
        assert all(q["datasource"] == {"type": "grafana", "uid": "-- Grafana --"}
                   for q in queries), stem
        # no `filter` key: an empty ids list reads as "show on no panels"
        assert all("filter" not in q for q in queries), stem


def test_module_source_has_no_em_dash():
    source = pathlib.Path(grafana_ops.__file__).read_text()
    assert "—" not in source


# --- subprocess lifecycle ----------------------------------------------------

class _FakeProc:
    """Enough of subprocess.Popen for _McpStdio: a stdin to write to, a stdout
    that ends immediately (so the handshake fails the way a server that dies
    mid-initialize does), and terminate/kill/wait that record being called."""

    def __init__(self, stdout_lines=(), terminate_raises=False):
        self.stdin = io.StringIO()
        self.stdout = iter(stdout_lines)
        self.stderr = iter(["mcp-grafana: boom\n"])
        self.terminate_raises = terminate_raises
        self.terminated = 0
        self.killed = 0
        self.waited = 0

    def terminate(self):
        self.terminated += 1
        if self.terminate_raises:
            raise OSError("terminate did not take")

    def kill(self):
        self.killed += 1

    def wait(self, timeout=None):
        self.waited += 1
        return 0


class _Spawned(list):
    """The fake processes Popen handed out, plus a knob for how the next one
    behaves."""

    kwargs: dict


@pytest.fixture
def spawned(monkeypatch):
    """Replace subprocess.Popen so no real mcp-grafana is started."""
    procs = _Spawned()
    procs.kwargs = {}

    def fake_popen(argv, **kwargs):
        proc = _FakeProc(**procs.kwargs)
        procs.append(proc)
        return proc

    monkeypatch.setattr(grafana_ops.subprocess, "Popen", fake_popen)
    procs.configure = procs.kwargs.update  # type: ignore[attr-defined]
    return procs


def test_handshake_failure_does_not_orphan_the_subprocess(spawned):
    # /bin/echo only has to satisfy the "is this runnable" check; Popen is faked
    ops = GrafanaOps(_Settings(), mcp_binary="/bin/echo")
    assert ops.mcp_tools == set()
    assert ops.mcp_error and "exited" in ops.mcp_error
    assert ops.mcp is None
    assert len(spawned) == 1, "the binary was runnable, so it should have spawned"
    assert spawned[0].terminated == 1, "spawned process left running after a failed handshake"
    assert spawned[0].waited >= 1, "terminated but never reaped"


def test_close_kills_and_reaps_when_terminate_does_not_take(spawned):
    spawned.configure(terminate_raises=True)
    GrafanaOps(_Settings(), mcp_binary="/bin/echo")
    proc = spawned[0]
    assert proc.terminated == 1
    assert proc.killed == 1
    assert proc.waited >= 1, "killed but never reaped, which leaves a zombie"


def test_mcp_close_is_safe_to_call_twice(spawned):
    client = grafana_ops._McpStdio("/bin/echo")
    try:
        client.start()
    except Exception:
        pass  # the handshake fails; the process is what matters
    proc = spawned[0]
    client.close()
    client.close()
    assert proc.terminated == 1, "the second close should be a no-op"


def test_close_on_a_client_that_never_started_is_a_no_op():
    grafana_ops._McpStdio("/nonexistent/mcp-grafana").close()


def test_grafana_ops_is_a_context_manager(spawned):
    with GrafanaOps(_Settings(), mcp_binary="/bin/echo") as ops:
        assert ops.mcp_tools == set()


# --- ensure_folder -----------------------------------------------------------

def _folder_calls(http):
    return [c for c in http if c["url"].endswith("/api/folders")]


def test_ensure_folder_http_treats_a_taken_uid_as_success(http):
    http.queue.append(_FakeResp(412, {"message": "the folder has been changed by someone else"}))
    ops = _ops(mcp_tools=())
    assert ops.ensure_folder() == "customs"
    assert len(_folder_calls(http)) == 1


def test_ensure_folder_http_raises_on_a_real_failure(http):
    http.queue.append(_FakeResp(400, {"message": "uid too long, max 40 characters"}))
    with pytest.raises(RuntimeError, match="uid too long"):
        _ops(mcp_tools=()).ensure_folder()


def test_ensure_folder_is_only_done_once(http):
    ops = _ops(mcp_tools=())
    ops.ensure_folder()
    ops.ensure_folder()
    assert len(_folder_calls(http)) == 1


def test_ensure_folder_mcp_conflict_is_success_and_never_touches_rest(http):
    # the real 1.1.0 error text for a uid that is already taken
    mcp = _FakeMcp(tools={"create_folder"}, results={"create_folder": {
        "isError": True,
        "content": [{"type": "text", "text":
                     "create folder 'Customs': [POST /folders] createFolder (status 412): {}"}],
    }})
    ops = _ops(mcp_tools={"create_folder"}, mcp=mcp)
    assert ops.ensure_folder() == "customs"
    assert _folder_calls(http) == []


def test_ensure_folder_mcp_other_error_falls_through_to_rest_and_raises(http):
    # the real 1.1.0 error text for a genuinely bad request
    mcp = _FakeMcp(tools={"create_folder"}, results={"create_folder": {
        "isError": True,
        "content": [{"type": "text", "text":
                     "create folder 'Bad': [POST /folders][400] createFolderBadRequest "
                     '{"message":"uid too long, max 40 characters"}'}],
    }})
    http.queue.append(_FakeResp(400, {"message": "uid too long, max 40 characters"}))
    ops = _ops(mcp_tools={"create_folder"}, mcp=mcp)
    with pytest.raises(RuntimeError) as excinfo:
        ops.ensure_folder()
    # both explanations survive into the raise
    assert "MCP create_folder said" in str(excinfo.value)
    assert "REST said" in str(excinfo.value)
    assert len(_folder_calls(http)) == 1, "a non-conflict MCP error must be re-checked over REST"


def test_ensure_folder_mcp_error_that_rest_says_is_fine_is_not_fatal(http):
    mcp = _FakeMcp(tools={"create_folder"}, results={"create_folder": {
        "isError": True,
        "content": [{"type": "text", "text": "connection reset"}],
    }})
    http.queue.append(_FakeResp(412, {"message": "already exists"}))
    ops = _ops(mcp_tools={"create_folder"}, mcp=mcp)
    assert ops.ensure_folder() == "customs"


def test_mcp_conflict_only_matches_a_conflict_status():
    assert grafana_ops._mcp_conflict("createFolder (status 412): {}")
    assert grafana_ops._mcp_conflict("[POST /folders][409] conflict")
    assert not grafana_ops._mcp_conflict("[POST /folders][400] createFolderBadRequest")
    assert not grafana_ops._mcp_conflict("rule not found")
    assert not grafana_ops._mcp_conflict("")
    # a bare number in prose is not a status
    assert not grafana_ops._mcp_conflict("deleted 412 stale annotations")


# --- public dashboard annotations exposure -----------------------------------

def test_enable_public_leaves_annotations_off_by_default(http):
    http.queue.append(_FakeResp(200, {"uid": "pd1", "accessToken": "tok"}))
    _ops(mcp_tools=()).enable_public("customs-overview")
    assert http[0]["json"]["annotationsEnabled"] is False


def test_enable_public_serves_annotations_only_when_asked(http):
    http.queue.append(_FakeResp(200, {"uid": "pd1", "accessToken": "tok"}))
    _ops(mcp_tools=()).enable_public("customs-timeline", annotations_enabled=True)
    assert http[0]["json"]["annotationsEnabled"] is True


def test_provision_shares_annotations_for_the_timeline_only():
    # the mapping the provisioning script applies, pinned here because the
    # difference between the two pages is a deliberate exposure decision
    import importlib.util

    root = pathlib.Path(grafana_ops.__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        "provision_grafana", root / "scripts" / "provision_grafana.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.PUBLIC_DASHBOARDS == {
        "customs-overview": False,
        "customs-timeline": True,
    }


# --- live, deselected by default (pytest addopts: -m 'not live') -------------

@pytest.mark.live
def test_mcp_tool_inventory_live():
    """The tool inventory is the thing the whole transport decision rests on,
    so it is taken against the real binary and the real stack, not asserted
    from a recorded list. Fails loudly if bin/mcp-grafana is missing, because
    HTTP-only mode would silently pass every other test in this file."""
    from customs.config import settings as live_settings

    ops = GrafanaOps(live_settings)
    try:
        assert ops.mcp_error is None, ops.mcp_error
        print(f"\nmcp-grafana tools: {len(ops.mcp_tools)}")
        for tool in ("update_dashboard", "alerting_manage_rules", "create_folder",
                     "get_panel_image", "query_loki_logs", "create_annotation"):
            assert tool in ops.mcp_tools, tool
        # read-only in 1.1.0: this is why ensure_contact_point is HTTP
        assert "alerting_manage_routing" in ops.mcp_tools
        assert ops.transport_for("ensure_dashboards") == "mcp"
        assert ops.transport_for("ensure_contact_point") == "http"
        print("transport:", ops.transport_report())
    finally:
        ops.close()


@pytest.mark.live
def test_query_history_live_reads_findings_back():
    """The read path (design spec section 9: the agent reads Grafana back).
    Runs over the same Loki data telemetry.push_log wrote, through whichever
    transport the live inventory selects."""
    from customs.config import settings as live_settings

    ops = GrafanaOps(live_settings)
    try:
        rows = ops.query_history("test_ad", "FR-ALC-01", days=30)
        print(f"\ntransport: {ops.transport_for('query_history')}, rows: {len(rows)}")
        assert rows, "no history for test_ad/FR-ALC-01: has the pipeline pushed?"
        for row in rows[:3]:
            print("  ", row["ts"], row["labels"].get("market"),
                  row["finding"] and row["finding"].get("severity"))
        assert all(r["finding"] and r["finding"]["rule_id"] == "FR-ALC-01" for r in rows)
    finally:
        ops.close()
