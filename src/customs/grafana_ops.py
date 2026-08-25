"""GrafanaOps: the Customs instrument panel, built by the agent that flies it.

Six dashboards, two alert rules, one webhook contact point, public embeds, and
the read path back out of Loki. Every write goes over the Grafana MCP server
when that server exposes a write tool for the operation, and over the Grafana
HTTP API when it does not. Which is which is not guessed: the tool inventory is
taken live at startup (MCP `tools/list`) and recorded in `self.mcp_tools`.

--- Live tool inventory, mcp-grafana v1.1.0, taken 2026-08-23 ---

`bin/mcp-grafana` (release binary `mcp-grafana_Darwin_arm64.tar.gz`, v1.1.0,
run as `mcp-grafana -t stdio` with env `GRAFANA_URL` +
`GRAFANA_SERVICE_ACCOUNT_TOKEN`) reported **73 tools** against this stack.
The ones this module cares about:

    update_dashboard        WRITE  create or replace a dashboard
    alerting_manage_rules   WRITE  create/update/delete/list/get alert rules
    create_folder           WRITE  create a folder (alert rules need one)
    create_annotation       WRITE  used by telemetry.py's sibling path
    get_panel_image         READ   render a panel or dashboard to PNG
    query_loki_logs         READ   LogQL range/instant query
    grafana_api_request     WRITE  generic REST passthrough (deliberately unused,
                                   see the note under MAPPING)
    alerting_manage_routing READ   contact points and notification policies are
                                   GET-only in 1.1.0: operations are exactly
                                   {get_notification_policies, get_contact_points,
                                    get_contact_point, get_time_intervals,
                                    get_time_interval}

There is no public-dashboard tool of any kind, and no tool that sets a rule
group's evaluation interval. Those two go over REST by necessity, not by
preference. See MAPPING below for the per-operation record.

--- The two clocks (inherited from telemetry.py, restated because panels
    depend on it) ---

`customs_risk` is written on the MAPPED clock: video second `n` lands at
wall-clock `t0 + n`, where `t0 = push_time - duration` is stored on the run
record. A panel showing it must be pinned to `[t0, t0 + duration]`, which is
what `embed_url` and `render_png` build. `customs_market_status`,
`customs_blocking` and `customs_stage_error` are current-clock and are what the
alert rules read; nothing here ever alerts on `customs_risk`.
"""
import base64
import json
import os
import queue
import re
import subprocess
import threading
import time
import warnings
from dataclasses import dataclass
from pathlib import Path

import httpx

# Datasource uids on the live stack (Grafana 13.2.0, verified 2026-08-23).
# Written verbatim into every dashboard panel and every alert query so nothing
# ever resolves against the org default datasource.
PROM_UID = "grafanacloud-prom"
LOKI_UID = "grafanacloud-logs"

_REPO_ROOT = Path(__file__).resolve().parents[2]
DASHBOARD_DIR = _REPO_ROOT / "grafana" / "dashboards"
# Dev binary: hand-downloaded, gitignored (bin/ never ships in a commit or an
# image), present on a developer's checkout. The Cloud Run image has no
# repo -- just /app -- and installs the linux/amd64 release binary at
# _CONTAINER_MCP_BINARY instead (see the Dockerfile). _default_mcp_binary
# below is what actually chooses between them at runtime.
MCP_BINARY = _REPO_ROOT / "bin" / "mcp-grafana"
_CONTAINER_MCP_BINARY = Path("/usr/local/bin/mcp-grafana")


def _default_mcp_binary() -> Path:
    """Resolve the mcp-grafana binary when the caller passes no explicit
    `mcp_binary`. Checked in order:

    1. `MCP_GRAFANA_BIN` env var -- an explicit override, for a deployment
       that installs the binary somewhere neither path below expects.
    2. `bin/mcp-grafana` (`MCP_BINARY`) -- the repo-relative dev binary.
       Present on a developer's checkout; absent in the deployed image.
    3. `/usr/local/bin/mcp-grafana` (`_CONTAINER_MCP_BINARY`) -- where the
       Dockerfile installs the linux/amd64 release binary. The fallback of
       last resort, and in the deployed image the only one of the three that
       is ever actually there.

    Returned whether or not anything exists at the result: `_McpStdio.start`
    raising `FileNotFoundError` for a binary that is still missing, caught by
    `_inventory` and turned into HTTP-only mode, is what handles "checked
    everywhere and it's still not there".
    """
    env = os.environ.get("MCP_GRAFANA_BIN")
    if env:
        return Path(env)
    if MCP_BINARY.exists():
        return MCP_BINARY
    return _CONTAINER_MCP_BINARY

# Alert rules and dashboards live in their own folder so the alert rule group
# (which is folder-scoped) has somewhere to be and the six dashboards are not
# scattered through the stack's default folder.
FOLDER_UID = "customs"
FOLDER_TITLE = "Customs"
ALERT_GROUP = "customs"
ALERT_GROUP_INTERVAL_S = 30
CONTACT_POINT_NAME = "customs-webhook"
ROUTE_LABEL = ("team", "=", "customs")

# embed_url / render_png need a run duration to close the mapped-clock window.
# RunRecord (schema.py) carries t0 but no duration, so the resolution order is:
# explicit `duration` argument, then a `duration` attribute on the run record if
# some later schema grows one, then this constant. 120s is the design spec's
# input cap (section 16), so the fallback window is guaranteed to *contain* the
# whole run rather than truncate it; the cost is trailing empty space on the
# right for a shorter ad, which is the safe direction to be wrong in. Callers
# that know the duration (the console does, it just measured the asset) should
# always pass it.
DEFAULT_EMBED_DURATION_S = 120.0


@dataclass(frozen=True)
class Transport:
    """One row of MAPPING: which MCP tool an operation needs before it is
    allowed to use MCP, and what it does over REST when that tool is absent."""
    mcp_tool: str | None
    http: str
    note: str = ""


# Per-operation transport record. `mcp_tool=None` means mcp-grafana 1.1.0 has no
# write tool for this operation at all, so it can never flip to MCP no matter
# what a future inventory contains. `grafana_api_request` is technically a
# universal write escape hatch, but routing an op through it would report "this
# ran over MCP" while doing exactly the REST call the HTTP branch does, with an
# extra process in between: an honest inventory beats a flattering one.
MAPPING: dict[str, Transport] = {
    "ensure_folder": Transport(
        "create_folder", "POST /api/folders"),
    "ensure_dashboards": Transport(
        "update_dashboard", "POST /api/dashboards/db"),
    "ensure_alert_rules": Transport(
        "alerting_manage_rules",
        "GET|POST|PUT /api/v1/provisioning/alert-rules",
        note="the rule group's 30s evaluation interval always goes over REST "
             "(PUT /api/v1/provisioning/folder/{uid}/rule-groups/customs): no "
             "MCP tool in 1.1.0 sets it",
    ),
    "ensure_contact_point": Transport(
        None, "GET|POST|PUT /api/v1/provisioning/contact-points",
        note="alerting_manage_routing is read-only in 1.1.0",
    ),
    "ensure_notification_policy": Transport(
        None, "GET|PUT /api/v1/provisioning/policies",
        note="alerting_manage_routing is read-only in 1.1.0",
    ),
    "enable_public": Transport(
        None, "POST|GET|PATCH /api/dashboards/uid/{uid}/public-dashboards",
        note="no public-dashboard tool exists in mcp-grafana 1.1.0",
    ),
    "render_png": Transport(
        "get_panel_image", "GET /render/d-solo/{uid}"),
    "query_history": Transport(
        "query_loki_logs",
        "GET /api/datasources/proxy/uid/grafanacloud-logs/loki/api/v1/query_range"),
}


# --- alert rules -------------------------------------------------------------
#
# Both rules read current-clock series only (see the module docstring's two
# clocks note). The `>= N` threshold lives inside the PromQL, exactly as the
# design spec writes it, and the Grafana condition is then a plain "the query
# returned something" threshold on top: PromQL's comparison operator filters the
# instant vector, so `> 0` on the result means "at least one series is at or
# over the line", and each surviving series becomes its own alert instance with
# its own {asset, market, rule_id} labels. Doing it the other way round (bare
# metric in PromQL, 70 in the Grafana evaluator) would work too but would put
# the number the spec pins in a place a reader does not look for it.

ALERT_RULES = [
    {
        "uid": "customs-blocking-finding",
        "title": "customs_blocking_finding",
        "expr": "max by (asset, market, rule_id) (customs_blocking) >= 70",
        "for": "0s",
        "group_by": ["asset", "market", "rule_id"],
        "labels": {"team": "customs", "severity": "critical"},
        "annotations": {
            "summary": (
                "Blocking finding: asset {{ $labels.asset }} in market "
                "{{ $labels.market }} trips rule {{ $labels.rule_id }} at "
                "severity {{ $value }}"
            ),
            "description": (
                "An unresolved, sourced legal finding at or above severity 70 is "
                "holding clearance. Resolve it back to zero by remediating the "
                "shot, or the market stays blocked."
            ),
        },
    },
    {
        "uid": "customs-market-at-risk",
        "title": "customs_market_at_risk",
        "expr": "max by (asset, market) (customs_market_status) >= 1",
        "for": "0s",
        "group_by": ["asset", "market"],
        "labels": {"team": "customs", "severity": "warning"},
        "annotations": {
            "summary": (
                "Market not cleared: asset {{ $labels.asset }} in market "
                "{{ $labels.market }} is at {{ $value }} (1 at risk, 2 blocked)"
            ),
            "description": (
                "customs_market_status left the cleared band. Open the market "
                "detail dashboard for this market to see which rule_id did it."
            ),
        },
    },
]


# --- the one HTTP seam -------------------------------------------------------
# Every REST call in this module funnels through _http, so a test doubles
# exactly one function to intercept dashboards, alert rules, contact points,
# public dashboards, rendering and Loki alike. Same pattern as telemetry._post.

def _http(method, url, *, headers, json_body=None, params=None, timeout=60.0):
    return httpx.request(
        method, url, headers=headers, json=json_body, params=params, timeout=timeout
    )


class _McpStdio:
    """JSON-RPC 2.0 client for an MCP server spoken over stdio.

    Implements exactly the three calls this project needs -- `initialize`,
    `tools/list`, `tools/call` -- plus the `notifications/initialized`
    notification the protocol requires between the first two. The server
    process is long-lived: one spawn serves every tool call until `close()`.

    Framing is newline-delimited JSON on stdout (mcp-grafana logs to stderr, so
    stdout stays clean). Responses are read by a background thread into a queue
    rather than by a blocking `readline`, so a server that dies mid-call
    surfaces as a timeout instead of a hung process.
    """

    PROTOCOL_VERSION = "2025-06-18"

    def __init__(self, binary, *, env=None, args=("-t", "stdio"), timeout=60.0):
        self.binary = str(binary)
        self.args = list(args)
        self.env = env
        self.timeout = timeout
        self._proc = None
        self._queue: queue.Queue = queue.Queue()
        self._stderr: list[str] = []
        self._id = 0

    # -- lifecycle --

    def start(self):
        if self._proc is not None:
            return self
        if not (os.path.isfile(self.binary) and os.access(self.binary, os.X_OK)):
            raise FileNotFoundError(f"mcp-grafana binary not runnable: {self.binary}")
        env = dict(os.environ)
        env.update(self.env or {})
        self._proc = subprocess.Popen(
            [self.binary] + self.args,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, text=True, bufsize=1,
        )
        threading.Thread(target=self._pump_stdout, daemon=True).start()
        threading.Thread(target=self._pump_stderr, daemon=True).start()
        self._handshake()
        return self

    def _pump_stdout(self):
        for line in self._proc.stdout:
            line = line.strip()
            if line:
                self._queue.put(line)
        self._queue.put(None)  # EOF sentinel

    def _pump_stderr(self):
        for line in self._proc.stderr:
            self._stderr.append(line.rstrip())
            del self._stderr[:-200]

    def close(self):
        """Stop the server process. Safe to call twice, and safe on a client
        that never started."""
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.close()
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            # terminate did not take (or the pipe was already broken): kill,
            # then reap, or the process stays a zombie until this process ends.
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.close()

    # -- JSON-RPC --

    def _send(self, message):
        if self._proc is None or self._proc.stdin is None:
            raise RuntimeError("mcp-grafana is not running")
        self._proc.stdin.write(json.dumps(message) + "\n")
        self._proc.stdin.flush()

    def _rpc(self, method, params=None):
        self._id += 1
        request_id = self._id
        message = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        self._send(message)
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"mcp-grafana did not answer {method} in {self.timeout}s")
            try:
                line = self._queue.get(timeout=remaining)
            except queue.Empty:
                raise TimeoutError(f"mcp-grafana did not answer {method} in {self.timeout}s")
            if line is None:
                tail = "\n".join(self._stderr[-5:])
                raise RuntimeError(f"mcp-grafana exited during {method}. stderr tail:\n{tail}")
            message = json.loads(line)
            # Server-initiated requests and notifications carry no matching id;
            # skip anything that is not the answer we are waiting for.
            if message.get("id") == request_id:
                if "error" in message:
                    raise RuntimeError(f"mcp-grafana error on {method}: {message['error']}")
                return message.get("result", {})

    def _notify(self, method, params=None):
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self._send(message)

    def _handshake(self):
        result = self._rpc("initialize", {
            "protocolVersion": self.PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "customs", "version": "0.1.0"},
        })
        self._notify("notifications/initialized", {})
        return result

    # -- the two calls the rest of the module uses --

    def list_tools(self) -> set[str]:
        self.start()
        names: set[str] = set()
        cursor = None
        while True:
            result = self._rpc("tools/list", {"cursor": cursor} if cursor else {})
            names.update(tool["name"] for tool in result.get("tools", []))
            cursor = result.get("nextCursor")
            if not cursor:
                return names

    def call_tool(self, name, arguments) -> dict:
        """Return the raw MCP result dict. A tool-level failure comes back as
        `{"isError": true, "content": [...]}` rather than an exception, because
        callers here need to tell "rule does not exist yet" (fine, create it)
        from "the server is broken" (not fine)."""
        self.start()
        return self._rpc("tools/call", {"name": name, "arguments": arguments})


def _mcp_text(result) -> str:
    """Concatenate the text blocks of an MCP tool result."""
    return "".join(
        block.get("text", "")
        for block in (result or {}).get("content", [])
        if block.get("type") == "text"
    )


def _mcp_failed(result) -> bool:
    return bool((result or {}).get("isError"))


# mcp-grafana flattens a Grafana API error into prose and keeps no structured
# status field, so "the uid is already taken" has to be read back out of the
# text. Both shapes seen live on 1.1.0, hence two patterns for the same idea:
#     exists   create folder 'Customs': [POST /folders] createFolder (status 412): {}
#     bad uid  create folder 'Bad': [POST /folders][400] createFolderBadRequest {...}
_MCP_STATUS = re.compile(r"(?:status\s+|\[)(\d{3})\b")


def _mcp_conflict(text: str) -> bool:
    """True only when an MCP tool error carries a 409 or 412, Grafana's answer
    for "that already exists". Anything else, including an unparseable error,
    is not treated as success."""
    return any(code in ("409", "412") for code in _MCP_STATUS.findall(text or ""))


class GrafanaOps:
    """Create and read back the Customs Grafana surface.

    Transport per operation is decided once, at construction, from the live MCP
    tool inventory (`self.mcp_tools`); `transport_for(op)` is the single place
    that decision is made and MAPPING is the record of it. With no binary, no
    inventory or an inventory missing a tool, the matching operation silently
    and completely falls back to the Grafana HTTP API, which is why every
    operation has a working REST path even when MCP is present.
    """

    def __init__(
        self,
        settings,
        *,
        mcp=None,
        mcp_tools=None,
        mcp_binary=None,
        dashboards_dir=None,
        folder_uid=FOLDER_UID,
        folder_title=FOLDER_TITLE,
        org_id=1,
        timeout=60.0,
    ):
        self.settings = settings
        self.org_id = org_id
        self.dashboards_dir = Path(dashboards_dir or DASHBOARD_DIR)
        self.folder_uid = folder_uid
        self.folder_title = folder_title
        self.timeout = timeout
        self.public_tokens: dict[str, str] = {}
        self.mcp_error: str | None = None
        self._folder_ready = False

        self.mcp = mcp
        if mcp_tools is not None:
            # Injected inventory: no process is started. Used by tests and by
            # any caller that already took the inventory once.
            self.mcp_tools = set(mcp_tools)
        elif mcp is not None:
            self.mcp_tools = self._inventory(mcp)
        else:
            binary = Path(mcp_binary) if mcp_binary else _default_mcp_binary()
            self.mcp = _McpStdio(binary, env={
                "GRAFANA_URL": settings.grafana_url,
                "GRAFANA_SERVICE_ACCOUNT_TOKEN": settings.grafana_sa_token,
            }, timeout=timeout)
            self.mcp_tools = self._inventory(self.mcp)

    def _inventory(self, mcp) -> set[str]:
        try:
            return set(mcp.list_tools())
        except Exception as exc:  # binary missing, handshake failed, timeout
            # start() spawns the process and only then handshakes, so anything
            # that fails after the spawn (a hung initialize, a server that
            # exits mid-handshake, a timeout) leaves a live subprocess behind.
            # Close it here: after this the client is dropped and nothing else
            # holds a handle to reap it.
            try:
                if hasattr(mcp, "close"):
                    mcp.close()
            except Exception:
                pass
            self.mcp = None
            self.mcp_error = f"{type(exc).__name__}: {exc}"
            warnings.warn(
                "mcp-grafana tool inventory failed, running HTTP-only "
                f"(the Publisher agent path needs MCP): {self.mcp_error}",
                stacklevel=3,
            )
            return set()

    def close(self):
        """Stop the mcp-grafana subprocess. Safe to call twice, and a no-op in
        HTTP-only mode."""
        if self.mcp is not None and hasattr(self.mcp, "close"):
            self.mcp.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- transport decision --

    def transport_for(self, op: str) -> str:
        """"mcp" if this operation has a matching MCP write/read tool in the
        live inventory, else "http". Raises KeyError for an unknown op so a
        typo cannot silently mean "use HTTP"."""
        spec = MAPPING[op]
        return "mcp" if spec.mcp_tool and spec.mcp_tool in self.mcp_tools else "http"

    def transport_report(self) -> dict[str, str]:
        """{op: "mcp:tool_name" | "http:METHOD /path"} for logs and reports."""
        report = {}
        for op, spec in MAPPING.items():
            if self.transport_for(op) == "mcp":
                report[op] = f"mcp:{spec.mcp_tool}"
            else:
                report[op] = f"http:{spec.http}"
        return report

    # -- HTTP plumbing --

    def _url(self, path: str) -> str:
        return self.settings.grafana_url.rstrip("/") + path

    def _headers(self, extra=None) -> dict:
        headers = {
            "Authorization": f"Bearer {self.settings.grafana_sa_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        headers.update(extra or {})
        return headers

    def _api(self, method, path, *, json_body=None, params=None, extra_headers=None):
        return _http(
            method, self._url(path), headers=self._headers(extra_headers),
            json_body=json_body, params=params, timeout=self.timeout,
        )

    def _api_json(self, method, path, *, json_body=None, params=None,
                  extra_headers=None, ok=(200, 201, 202)):
        resp = self._api(method, path, json_body=json_body, params=params,
                         extra_headers=extra_headers)
        if resp.status_code not in ok:
            raise RuntimeError(
                f"grafana {method} {path} failed: HTTP {resp.status_code}: {resp.text[:500]}"
            )
        try:
            return resp.json()
        except Exception:
            return {}

    def _mcp_call(self, tool, arguments) -> dict:
        if self.mcp is None:
            raise RuntimeError(f"MCP transport chosen for {tool} but no client is running")
        return self.mcp.call_tool(tool, arguments)

    # -- folder --

    def ensure_folder(self) -> str:
        """Create the Customs folder if it is not there.

        Idempotent, but only for the one error that means "the uid is already
        taken": REST answers 409 or 412 for that (verified live: an existing
        folder answers 412), and MCP answers with an isError result whose text
        carries the same status. Every other failure is a real one. An MCP
        failure that is not recognisably a conflict is not assumed to be
        either way: the create is repeated over REST, which reports an exact
        status and message, and the raise carries both explanations.
        """
        if self._folder_ready:
            return self.folder_uid
        if self.transport_for("ensure_folder") == "mcp":
            answer = self._mcp_call("create_folder", {
                "title": self.folder_title, "uid": self.folder_uid,
            })
            if _mcp_failed(answer):
                text = _mcp_text(answer)
                if not _mcp_conflict(text):
                    self._create_folder_http(mcp_error=text)
        else:
            self._create_folder_http()
        self._folder_ready = True
        return self.folder_uid

    def _create_folder_http(self, mcp_error: str | None = None) -> None:
        resp = self._api("POST", "/api/folders", json_body={
            "uid": self.folder_uid, "title": self.folder_title,
        })
        # 409 or 412 is Grafana saying the uid is taken, which is the success
        # case here.
        if resp.status_code in (200, 201, 409, 412):
            return
        detail = f"HTTP {resp.status_code}: {resp.text[:300]}"
        if mcp_error:
            detail = f"MCP create_folder said: {mcp_error[:300]} | REST said: {detail}"
        raise RuntimeError(f"could not create folder {self.folder_uid}: {detail}")

    # -- dashboards --

    def ensure_dashboards(self) -> dict[str, str]:
        """Push every JSON file in `grafana/dashboards/` and return
        {file stem: dashboard uid}. The uid is taken from the file, never
        generated, so `customs-timeline` is `customs-timeline` on every stack
        and an embed URL written today still resolves after a reprovision."""
        self.ensure_folder()
        result: dict[str, str] = {}
        use_mcp = self.transport_for("ensure_dashboards") == "mcp"
        for path in sorted(self.dashboards_dir.glob("*.json")):
            dashboard = json.loads(path.read_text())
            uid = dashboard["uid"]
            body = {
                "dashboard": dashboard,
                "folderUid": self.folder_uid,
                "overwrite": True,
                "message": "customs provisioning",
            }
            if use_mcp:
                answer = self._mcp_call("update_dashboard", body)
                if _mcp_failed(answer):
                    raise RuntimeError(
                        f"update_dashboard failed for {uid}: {_mcp_text(answer)[:500]}"
                    )
            else:
                self._api_json("POST", "/api/dashboards/db", json_body=body)
            result[path.stem] = uid
        return result

    # -- alert rules --

    def _rule_body(self, rule) -> dict:
        """Grafana provisioning AlertRule payload for one ALERT_RULES entry."""
        return {
            "uid": rule["uid"],
            "title": rule["title"],
            "condition": "B",
            "data": self._rule_data(rule),
            "folderUID": self.folder_uid,
            "ruleGroup": ALERT_GROUP,
            "for": rule["for"],
            "noDataState": "OK",
            "execErrState": "Alerting",
            "labels": dict(rule["labels"]),
            "annotations": dict(rule["annotations"]),
            "isPaused": False,
        }

    @staticmethod
    def _rule_data(rule) -> list[dict]:
        return [
            {
                "refId": "A",
                "datasourceUid": PROM_UID,
                "relativeTimeRange": {"from": 600, "to": 0},
                "model": {
                    "refId": "A",
                    "expr": rule["expr"],
                    "instant": True,
                    "range": False,
                    "editorMode": "code",
                    "intervalMs": 1000,
                    "maxDataPoints": 43200,
                    "datasource": {"type": "prometheus", "uid": PROM_UID},
                },
            },
            {
                "refId": "B",
                "datasourceUid": "__expr__",
                "relativeTimeRange": {"from": 600, "to": 0},
                "model": {
                    "refId": "B",
                    "type": "threshold",
                    "expression": "A",
                    "conditions": [{
                        "type": "query",
                        "evaluator": {"type": "gt", "params": [0]},
                        "operator": {"type": "and"},
                        "query": {"params": ["A"]},
                        "reducer": {"type": "last", "params": []},
                    }],
                    "datasource": {"type": "__expr__", "uid": "__expr__"},
                },
            },
        ]

    def ensure_alert_rules(self) -> dict[str, str]:
        """Create or update both Customs alert rules and pin the group's
        evaluation interval to 30s. Returns {rule title: rule uid}.

        Uids are fixed strings, so a reprovision updates the same two rules
        instead of piling up copies. The group interval is the one part that
        always goes over REST (see MAPPING): mcp-grafana 1.1.0 can write a rule
        but has no tool that touches the group it lives in.
        """
        self.ensure_folder()
        use_mcp = self.transport_for("ensure_alert_rules") == "mcp"
        created: dict[str, str] = {}
        for rule in ALERT_RULES:
            if use_mcp:
                self._upsert_rule_mcp(rule)
            else:
                self._upsert_rule_http(rule)
            created[rule["title"]] = rule["uid"]
        self._set_group_interval(ALERT_GROUP_INTERVAL_S)
        return created

    def _upsert_rule_mcp(self, rule):
        existing = self._mcp_call("alerting_manage_rules", {
            "operation": "get", "rule_uid": rule["uid"], "org_id": self.org_id,
        })
        operation = "update" if not _mcp_failed(existing) else "create"
        args = {
            "operation": operation,
            # alerting_manage_rules refuses a write without an explicit org_id
            # ("org_id is required and must be greater than 0"), unlike every
            # other tool, which reads the org off the service account token.
            "org_id": self.org_id,
            "rule_uid": rule["uid"],
            "title": rule["title"],
            "folder_uid": self.folder_uid,
            "rule_group": ALERT_GROUP,
            "condition": "B",
            "data": self._rule_data(rule),
            "for": rule["for"],
            "no_data_state": "OK",
            "exec_err_state": "Alerting",
            "labels": dict(rule["labels"]),
            "annotations": dict(rule["annotations"]),
            "disable_provenance": True,
        }
        answer = self._mcp_call("alerting_manage_rules", args)
        if _mcp_failed(answer):
            raise RuntimeError(
                f"alerting_manage_rules {operation} failed for {rule['title']}: "
                f"{_mcp_text(answer)[:500]}"
            )

    def _upsert_rule_http(self, rule):
        existing = self._api("GET", f"/api/v1/provisioning/alert-rules/{rule['uid']}")
        body = self._rule_body(rule)
        headers = {"X-Disable-Provenance": "true"}
        if existing.status_code == 200:
            self._api_json("PUT", f"/api/v1/provisioning/alert-rules/{rule['uid']}",
                           json_body=body, extra_headers=headers)
        else:
            self._api_json("POST", "/api/v1/provisioning/alert-rules",
                           json_body=body, extra_headers=headers)

    def _set_group_interval(self, seconds: int):
        """Read the group back and PUT it with the interval changed. The PUT
        replaces the whole group, so the rules it currently holds have to ride
        along or provisioning deletes them."""
        path = f"/api/v1/provisioning/folder/{self.folder_uid}/rule-groups/{ALERT_GROUP}"
        group = self._api_json("GET", path)
        body = {
            "title": group.get("title", ALERT_GROUP),
            "folderUid": self.folder_uid,
            "interval": seconds,
            "rules": group.get("rules", []),
        }
        self._api_json("PUT", path, json_body=body,
                       extra_headers={"X-Disable-Provenance": "true"})

    # -- contact point and routing --

    def ensure_contact_point(self, webhook_url: str) -> str:
        """Create or update the `customs-webhook` contact point and route
        `team=customs` alerts to it. Returns the contact point uid.

        Both halves are REST: `alerting_manage_routing` in mcp-grafana 1.1.0 is
        read-only. Routing is done by label match rather than by attaching a
        receiver to each rule (`notification_settings`), so adding a third rule
        later only means labelling it `team=customs`.
        """
        headers = {"X-Disable-Provenance": "true"}
        points = self._api_json("GET", "/api/v1/provisioning/contact-points")
        existing = next(
            (p for p in points if p.get("name") == CONTACT_POINT_NAME), None
        ) if isinstance(points, list) else None
        body = {
            "name": CONTACT_POINT_NAME,
            "type": "webhook",
            "disableResolveMessage": False,
            "settings": {"url": webhook_url, "httpMethod": "POST"},
        }
        if existing:
            body["uid"] = existing["uid"]
            self._api_json("PUT", f"/api/v1/provisioning/contact-points/{existing['uid']}",
                           json_body=body, extra_headers=headers)
            uid = existing["uid"]
        else:
            answer = self._api_json("POST", "/api/v1/provisioning/contact-points",
                                    json_body=body, extra_headers=headers)
            uid = answer.get("uid", "")
        self.ensure_notification_policy(CONTACT_POINT_NAME)
        return uid

    def ensure_notification_policy(self, receiver: str = CONTACT_POINT_NAME) -> dict:
        """Put a child route for `team=customs` at the top of the policy tree,
        keeping every other route the stack already has. Any previous route to
        the same receiver is replaced rather than added to, so reprovisioning
        does not grow a stack of identical routes."""
        tree = self._api_json("GET", "/api/v1/provisioning/policies")
        label, operator, value = ROUTE_LABEL
        route = {
            "receiver": receiver,
            "object_matchers": [[label, operator, value]],
            "continue": False,
            # grafana_folder and alertname are required members of any explicit
            # group_by; the three Customs labels after them mean one webhook
            # call per (asset, market, rule_id), which is one per finding.
            "group_by": ["grafana_folder", "alertname", "asset", "market", "rule_id"],
            "group_wait": "10s",
            "group_interval": "30s",
            "repeat_interval": "1h",
        }
        others = [r for r in (tree.get("routes") or []) if r.get("receiver") != receiver]
        tree["routes"] = [route] + others
        self._api_json("PUT", "/api/v1/provisioning/policies", json_body=tree,
                       extra_headers={"X-Disable-Provenance": "true"})
        return tree

    # -- public dashboards --

    def enable_public(self, uid: str, *, annotations_enabled: bool = False) -> str:
        """Turn on public sharing for one dashboard and return its public URL
        (`{grafana_url}/public-dashboards/{accessToken}`). The access token is
        also cached in `self.public_tokens[uid]` for `embed_url`.

        REST only: mcp-grafana 1.1.0 has no public-dashboard tool. Idempotent:
        a dashboard that is already public answers the POST with a 400/409, and
        the existing config is then read back and PATCHed instead.

        **A public dashboard has no login.** Anyone with the link reads the
        panels, and that is deliberate here: these pages are the demo's
        judge-facing surface and the findings on them are demo data about a
        synthetic test asset. Say it plainly rather than leave it implied.

        `annotations_enabled` is a separate decision from sharing, because
        annotations are served by their own public endpoint
        (`/api/public/dashboards/{token}/annotations`) whether or not any panel
        on the page draws them: leaving it on for a page that does not render
        annotations publishes finding text nothing asked for. It defaults to
        off. Pass `annotations_enabled=True` for the timeline, where the
        finding and remediation markers are half the point of the page.
        """
        body = {
            "isEnabled": True,
            "share": "public",
            "timeSelectionEnabled": True,
            "annotationsEnabled": bool(annotations_enabled),
        }
        path = f"/api/dashboards/uid/{uid}/public-dashboards"
        resp = self._api("POST", path, json_body=body)
        if resp.status_code in (200, 201, 202):
            config = resp.json()
        # Verified live: a dashboard that is already public answers the POST
        # with 400 "Dashboard is already public".
        elif resp.status_code in (400, 409):
            config = self._api_json("GET", path)
            config = self._api_json(
                "PATCH", f"{path}/{config['uid']}",
                json_body={**body, "uid": config["uid"]},
            ) or config
        else:
            raise RuntimeError(
                f"enable_public failed for {uid}: HTTP {resp.status_code}: {resp.text[:300]}"
            )
        token = config.get("accessToken")
        if not token:
            raise RuntimeError(f"enable_public got no accessToken for {uid}: {config}")
        self.public_tokens[uid] = token
        return self.public_url(token)

    def create_adhoc_dashboard(self, dashboard: dict) -> dict:
        """Push a dashboard nobody wrote in advance.

        The provisioned six live in grafana/dashboards/ and answer questions
        we knew about. This one is composed at request time from the labels a
        run already pushed, which is the only way "show me the errors by
        dimension" can be answered without someone having anticipated the
        question. Same folder, same overwrite semantics, same transport
        choice as ensure_dashboards; the difference is where the JSON came
        from, not how it lands.
        """
        self.ensure_folder()
        body = {
            "dashboard": dashboard,
            "folderUid": self.folder_uid,
            "overwrite": True,
            "message": "customs agent, composed on request",
        }
        if self.transport_for("ensure_dashboards") == "mcp":
            try:
                self._mcp_call("update_dashboard", {"dashboard": dashboard,
                                                    "folderUid": self.folder_uid,
                                                    "overwrite": True})
            except Exception:  # noqa: BLE001 -- REST is the documented fallback
                self._api_json("POST", "/api/dashboards/db", json_body=body)
        else:
            self._api_json("POST", "/api/dashboards/db", json_body=body)
        uid = dashboard["uid"]
        return {"uid": uid,
                "url": self._url(f"/d/{uid}"),
                "title": dashboard.get("title", uid)}

    def public_url(self, token: str) -> str:
        return f"{self.settings.grafana_url.rstrip('/')}/public-dashboards/{token}"

    def refresh_public_tokens(self) -> dict[str, str]:
        """Fill `self.public_tokens` from the stack, so a fresh process can
        build embed URLs for dashboards a previous run made public.

        Kept separate from `embed_url` on purpose: embed_url is pure string
        building and must never make a network call, because the console builds
        embed URLs inside a request handler.
        """
        answer = self._api_json("GET", "/api/dashboards/public-dashboards")
        rows = answer.get("publicDashboards", answer) if isinstance(answer, dict) else answer
        for row in rows or []:
            if row.get("dashboardUid") and row.get("accessToken"):
                self.public_tokens[row["dashboardUid"]] = row["accessToken"]
        return dict(self.public_tokens)

    # -- embeds --

    def _window_ms(self, run, duration=None) -> tuple[int, int]:
        if run.t0 is None:
            raise ValueError(
                f"run {run.id!r} has no t0: call telemetry.push_timeline before "
                "building an embed, or the panel window has no mapped clock to sit on"
            )
        if duration is None:
            duration = getattr(run, "duration", None)
        if duration is None:
            duration = DEFAULT_EMBED_DURATION_S
        return int(run.t0 * 1000), int((run.t0 + float(duration)) * 1000)

    def embed_url(self, uid: str, panel_id, run, duration=None, public_token=None) -> str:
        """URL for an iframe showing one panel over this run's mapped window.

        Two shapes, and which one you get depends only on whether a public
        access token is known for `uid`:

          public   {grafana_url}/public-dashboards/{token}?from=..&to=..&viewPanel={panel_id}
          private  {grafana_url}/d-solo/{uid}?panelId={panel_id}&from=..&to=..

        The public shape is the primary path (design spec section 9, "the embed
        auth problem"): an iframe cannot carry a bearer token, so a private
        d-solo URL only renders for a browser that already has a Grafana
        session. The private shape is still returned when no token is known, so
        a logged-in operator gets a working link instead of nothing.

        `from`/`to` are `t0` and `t0 + duration` in epoch milliseconds, which is
        the run's mapped clock and therefore reads as the ad's timecode. See
        DEFAULT_EMBED_DURATION_S for how `duration` is resolved. No network
        access happens here; the token comes from `self.public_tokens`, filled
        by `enable_public` or `refresh_public_tokens`.
        """
        from_ms, to_ms = self._window_ms(run, duration)
        token = public_token or self.public_tokens.get(uid)
        base = self.settings.grafana_url.rstrip("/")
        if token:
            return (f"{base}/public-dashboards/{token}"
                    f"?from={from_ms}&to={to_ms}&viewPanel={panel_id}")
        return f"{base}/d-solo/{uid}?panelId={panel_id}&from={from_ms}&to={to_ms}"

    # --- Grafana as a data source for the app's own UI ----------------
    #
    # An iframe is not available: Grafana Cloud answers with
    # x-frame-options: deny. A server-rendered PNG works and is what the
    # big panels use, but it is an image -- it cannot be small, sharp,
    # theme-aware and instant, which is exactly what a chart INSIDE a card
    # has to be.
    #
    # So the third way: the numbers come from Grafana, the drawing happens
    # here. Mimir and Loki stay the source of truth and the MCP agent
    # queries the same series, while the card renders inline SVG in the
    # product's own hex. Nothing to embed, nothing to theme-match, no
    # second round trip from the browser.

    def prom_range(self, query: str, seconds: int = 86400, step: int = 60
                   ) -> list[dict]:
        """A range query against Mimir, as [{labels, points:[(ts, value)]}]."""
        now = time.time()
        answer = self._api_json(
            "GET", "/api/datasources/proxy/uid/" + PROM_UID + "/api/v1/query_range",
            params={"query": query, "start": str(int(now - seconds)),
                    "end": str(int(now)), "step": str(step)})
        out = []
        for row in (answer.get("data") or {}).get("result") or []:
            pts = []
            for ts, val in row.get("values") or []:
                try:
                    pts.append((float(ts), float(val)))
                except (TypeError, ValueError):
                    continue
            out.append({"labels": row.get("metric") or {}, "points": pts})
        return out

    def prom_window(self, query: str, start: float, end: float,
                    points: int = 48) -> list[dict]:
        """A range query over an EXPLICIT window, as [{labels, points}].

        customs_risk lives on each run's mapped clock -- a twenty second
        window somewhere in the afternoon, not wall time -- so a card's
        sparkline has to ask for that run's own span or it gets one point.
        """
        step = max(1, int((end - start) / max(points, 1)))
        answer = self._api_json(
            "GET", "/api/datasources/proxy/uid/" + PROM_UID + "/api/v1/query_range",
            params={"query": query, "start": str(int(start)),
                    "end": str(int(end)), "step": str(step)})
        out = []
        for row in (answer.get("data") or {}).get("result") or []:
            pts = []
            for ts, val in row.get("values") or []:
                try:
                    pts.append((float(ts), float(val)))
                except (TypeError, ValueError):
                    continue
            out.append({"labels": row.get("metric") or {}, "points": pts})
        return out

    def prom_instant(self, query: str) -> list[dict]:
        """An instant query against Mimir, as [{labels, value}]."""
        answer = self._api_json(
            "GET", "/api/datasources/proxy/uid/" + PROM_UID + "/api/v1/query",
            params={"query": query, "time": str(int(time.time()))})
        out = []
        for row in (answer.get("data") or {}).get("result") or []:
            try:
                out.append({"labels": row.get("metric") or {},
                            "value": float((row.get("value") or [0, "0"])[1])})
            except (TypeError, ValueError):
                continue
        return out

    def loki_instant(self, query: str) -> list[dict]:
        """An instant LogQL metric query, as [{labels, value}]."""
        answer = self._api_json(
            "GET", "/api/datasources/proxy/uid/" + LOKI_UID + "/loki/api/v1/query",
            params={"query": query, "time": str(int(time.time() * 1e9))})
        out = []
        for row in (answer.get("data") or {}).get("result") or []:
            try:
                out.append({"labels": row.get("metric") or {},
                            "value": float((row.get("value") or [0, "0"])[1])})
            except (TypeError, ValueError):
                continue
        return out

    def render_png(self, uid: str, panel_id, run, duration=None, *,
                   width=1000, height=500, variables=None, theme="dark",
                   window_ms: tuple[int, int] | None = None) -> bytes:
        """Server-side render of a panel (or the whole dashboard when
        `panel_id` is None) over the run's mapped window, as PNG bytes.

        This is the fallback half of the embed pair: it loses interactivity but
        cannot fail on panel type support or on iframe auth, because the render
        happens inside Grafana with the service account token. MCP
        `get_panel_image` when available (it returns base64 image content),
        otherwise the `/render` endpoint directly.
        """
        # A run's panels sit on its mapped clock, which is a handful of
        # seconds wide. A dashboard composed from wall-clock log labels does
        # not: rendering it over the run's window shows twenty seconds of an
        # empty afternoon, which is exactly what "No data" looked like.
        from_ms, to_ms = window_ms if window_ms else self._window_ms(run, duration)
        if self.transport_for("render_png") == "mcp":
            args = {
                "dashboardUid": uid,
                "width": width,
                "height": height,
                "theme": theme,
                # Epoch milliseconds as strings, not RFC3339. get_panel_image
                # documents RFC3339 as acceptable, but on mcp-grafana 1.1.0 an
                # RFC3339 bound renders an empty window (verified 2026-08-23: an
                # RFC3339 pair produced a "No data" panel and, on a whole
                # dashboard render, a 1970-01-01 time picker, while the same
                # window as epoch millis rendered correctly). Millis are what
                # Grafana's own from/to take anyway.
                "timeRange": {"from": str(from_ms), "to": str(to_ms)},
            }
            if panel_id is not None:
                args["panelId"] = int(panel_id)
            if variables:
                args["variables"] = {f"var-{k}": v for k, v in variables.items()}
            answer = self._mcp_call("get_panel_image", args)
            if _mcp_failed(answer):
                raise RuntimeError(f"get_panel_image failed for {uid}: {_mcp_text(answer)[:500]}")
            for block in answer.get("content", []):
                if block.get("data"):
                    return base64.b64decode(block["data"])
            raise RuntimeError(f"get_panel_image returned no image for {uid}: "
                               f"{_mcp_text(answer)[:300]}")

        # kiosk strips the nav bar and the assistant banner from a whole
        # dashboard render (verified live: without it the PNG carries Grafana's
        # top chrome). It is a no-op on a d-solo panel render.
        params = {"from": from_ms, "to": to_ms, "width": width, "height": height,
                  "tz": "UTC", "scale": 1, "theme": theme, "kiosk": "true"}
        if panel_id is not None:
            params["panelId"] = int(panel_id)
        for key, value in (variables or {}).items():
            params[f"var-{key}"] = value
        kind = "d-solo" if panel_id is not None else "d"
        resp = _http("GET", self._url(f"/render/{kind}/{uid}/customs"),
                     headers=self._headers({"Accept": "image/png"}),
                     params=params, timeout=max(self.timeout, 120.0))
        if resp.status_code != 200:
            raise RuntimeError(
                f"panel render failed for {uid}: HTTP {resp.status_code}: {resp.text[:300]}"
            )
        return resp.content

    # -- reading Grafana back --

    def query_history(self, brand: str, rule_id: str, *, days=30, limit=100) -> list[dict]:
        """Every past finding for this brand on this rule, newest first.

        This is the third traffic direction from design spec section 9: before
        remediating, the Remediator asks Grafana what happened last time. The
        Loki label for "which asset" is `asset` (telemetry.py writes the asset
        file stem), so a brand is matched as a substring of it.

        Each row: {"ts", "ts_ns", "labels", "line", "finding"}, where `finding`
        is the parsed finding JSON that telemetry.push_log wrote as the line
        body, or None when a line will not parse (kept rather than dropped: a
        malformed line in the history is itself worth seeing).
        """
        logql = (f'{{app="customs", asset=~".*{re.escape(brand)}.*", '
                 f'rule_id="{rule_id}"}}')
        if self.transport_for("query_history") == "mcp":
            answer = self._mcp_call("query_loki_logs", {
                "datasourceUid": LOKI_UID,
                "logql": logql,
                "startRfc3339": f"now-{days}d",
                "endRfc3339": "now",
                "direction": "backward",
                "limit": limit,
            })
            if _mcp_failed(answer):
                raise RuntimeError(f"query_loki_logs failed: {_mcp_text(answer)[:500]}")
            return _rows_from_mcp(_mcp_text(answer))

        now = time.time()
        answer = self._api_json(
            "GET",
            "/api/datasources/proxy/uid/" + LOKI_UID + "/loki/api/v1/query_range",
            params={
                "query": logql,
                "start": str(int((now - days * 86400) * 1e9)),
                "end": str(int(now * 1e9)),
                "limit": limit,
                "direction": "backward",
            },
        )
        return _rows_from_loki(answer)


def _rfc3339(unix_seconds: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(unix_seconds))


def _normalize_ts(raw) -> tuple[str | None, str | None]:
    """(rfc3339, nanosecond digits) from whatever a transport calls a
    timestamp. mcp-grafana hands back the Loki timestamp as a JSON-quoted
    nanosecond string (literally `"1787494319324578048"`, quotes included);
    the datasource proxy hands back bare digits. query_history's rows are a
    contract other tasks read, so both transports normalize to one shape."""
    if raw is None:
        return None, None
    text = str(raw).strip().strip('"')
    if text.isdigit():
        return _rfc3339(int(text) / 1e9), text
    return text or None, None


def _row(raw_ts, labels, line) -> dict:
    try:
        finding = json.loads(line)
        if not isinstance(finding, dict):
            finding = None
    except (ValueError, TypeError):
        finding = None
    ts, ts_ns = _normalize_ts(raw_ts)
    return {"ts": ts, "ts_ns": ts_ns, "labels": labels or {}, "line": line,
            "finding": finding}


def _rows_from_mcp(text: str) -> list[dict]:
    """mcp-grafana returns query_loki_logs results as a JSON array of
    {timestamp, line, labels}. Anything else (an empty result rendered as
    prose, a wrapper object) yields no rows rather than an exception."""
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        return []
    if isinstance(payload, dict):
        payload = payload.get("data") or payload.get("results") or []
    if not isinstance(payload, list):
        return []
    return [
        _row(entry.get("timestamp"), entry.get("labels"), entry.get("line", ""))
        for entry in payload if isinstance(entry, dict)
    ]


def _rows_from_loki(answer: dict) -> list[dict]:
    rows = []
    for stream in answer.get("data", {}).get("result", []):
        labels = stream.get("stream", {})
        for ts_ns, line in stream.get("values", []):
            rows.append(_row(ts_ns, labels, line))
    return rows
