"""Push Customs telemetry into Grafana Cloud: the mapped-clock risk timeline,
current-clock alerting series, Loki finding detail, and Grafana annotations.

--- The timecode-as-time-axis trick (design spec section 9) ---

`customs_risk{asset,market,dimension}` is sampled once per video second and
is meant to be read on a Grafana time-series/heatmap panel where the x-axis
*is* the ad's timecode. Prometheus/Mimir samples must be written in roughly
real time (out-of-order and future tolerance both bounded), so a video
second cannot be written at its own literal offset from epoch 0. Instead
each run picks one `t0` and writes video second `n` at wall-clock `t0 + n`:
samples land on a real, monotonic clock, and the panel's time range
`[t0, t0 + duration]` reads as the timecode because it is the timecode,
offset by a constant.

--- Backdate probe result (scripts/probe_backdate.py, run live 2026-08-23) ---

Pushed customs_backdate_probe samples at push_time-2m/-5m/-10m/+30s/+90s
(distinct probe_offset label per sample), slept 20s, queried each back
through the datasource proxy with `time=` pinned to the sample's own
timestamp (so query timing can never masquerade as a rejection). Result:
ALL FIVE offsets were accepted -- HTTP 200 on push and a correct value on
query-back, including push_time-10m, the most backdated sample tested.

    offset       push_http   query_ok   verdict
    -120s (2m)   200         True       ACCEPTED
    -300s (5m)   200         True       ACCEPTED
    -600s (10m)  200         True       ACCEPTED
    +30s         200         True       ACCEPTED
    +90s         200         True       ACCEPTED

Decision rule (task-11-brief.md): past acceptance already covers 10 minutes
(600s), comfortably over "2 minutes plus duration" for any duration up to
the spec's 120s input cap (need >= 240s; have >= 600s confirmed). That
satisfies the FIRST branch of the rule, so per "implement whichever branch
the probe selects; do not implement the others" this file implements ONLY:

    t0 = push_time - duration

Consequence worth spelling out: with this formula every mapped sample
timestamp `t0 + n` (n in [0, duration]) is <= push_time, i.e. every sample
Customs ever writes lands at or before the moment it is pushed -- the
future-offset branch is never exercised by this strategy, which is the
safer of the two accepted directions and is why it was preferred once the
past window alone already cleared the bar. The Loki-only fallback branch
(heatmap via LogQL) was not implemented; it was never selected.

--- The alerting/heatmap split (design constraint, repeated per task brief) ---

ALERTING reads ONLY current-clock series: `customs_market_status`,
`customs_blocking`, `customs_stage_error`. These are stamped at real
`time.time()` when pushed, so a Grafana alert rule's evaluation window
(e.g. "over the last 5m") means what it says.

The heatmap (Timeline page) reads ONLY the mapped-clock series:
`customs_risk`. Its samples are NOT current -- they are backdated on
purpose to draw the timecode axis -- so an alert rule pointed at
`customs_risk` would fire on stale-looking data or never fire at all.

Never mix the two. Every push_* function below is unambiguously one or the
other; see each docstring.
"""
import json
import math
import time
from pathlib import Path

import httpx

from customs.adjudicate import CLEARANCE_SEVERITY_THRESHOLD
from customs.config import settings
from customs.packs import load as load_packs
from customs.schema import ChangeRecord, Finding, RunRecord
from customs.store import Store

# clearance() string -> customs_market_status gauge value (task-11-brief.md).
_CLEARANCE_CODE = {"cleared": 0, "at_risk": 1, "blocked": 2}

# --- lazy singletons, mirroring genai_client.py's client() pattern ---
# push_timeline and annotate_resolution need a Store (to call
# set_run_t0, and to resolve a ChangeRecord's finding_id back to a Finding)
# but neither is in the literal interface signature from task-11-brief.md.
# Both take an optional trailing `store` param instead of a hidden import-time
# dependency: the brief-literal call form (positional args only) still works
# unchanged via this default, and a test or a future caller that already
# holds a Store can inject it instead of standing up a second connection to
# the same sqlite file.
_store_singleton: Store | None = None

def _store() -> Store:
    global _store_singleton
    if _store_singleton is None:
        _store_singleton = Store(settings.db_path)
    return _store_singleton

# customs_risk needs a `dimension` label per sample (task-11-brief.md), but
# Finding (schema.py) does not carry dimension -- only the Observation it was
# judged from does, and push_timeline is not given observations. dimension
# is looked up from the market pack instead: rule_id -> MarketRule.dimension,
# the same join adjudicate.judge() uses when it drops dimension while
# building the Finding in the first place. Loaded once, lazily (pure local
# YAML read, same as pipeline.run's own `load_packs()` call).
_packs_cache: dict | None = None

def _dimension_for(market: str, rule_id: str) -> str:
    global _packs_cache
    if _packs_cache is None:
        _packs_cache = load_packs()
    pack = _packs_cache.get(market)
    if pack is None:
        return "none"
    for rule in pack.rules:
        if rule.id == rule_id:
            return rule.dimension
    return "none"

def _asset_label(run: RunRecord) -> str:
    """Prometheus/Loki label value for 'this asset'. The schema has no
    asset_id (only RunRecord.asset_path, a filesystem path) -- the file
    stem is used as a short, readable stand-in, e.g. "docs/samples/test_ad.mp4"
    -> "test_ad"."""
    return Path(run.asset_path).stem or run.asset_path

def _mapped_unix_seconds(run: RunRecord, video_t: float) -> float:
    """video-second offset -> wall-clock unix seconds on this run's mapped
    clock. Requires run.t0 to already be set (push_timeline sets it); raises
    rather than silently falling back to a current-clock guess, because a
    Loki line or annotation on the wrong clock is exactly the "never mix"
    bug the module docstring warns about."""
    if run.t0 is None:
        raise ValueError(
            f"run {run.id!r} has no t0 set -- call push_timeline before "
            "push_log/annotate/annotate_resolution"
        )
    return run.t0 + video_t

# --- one HTTP seam, so tests need to monkeypatch exactly one function ---
# (task-11-brief.md: "httpx mocked via monkeypatching a module-level _post
# function"). _otlp_push, _loki_push and _annotation_post all funnel through
# this, so a test double only ever has to fake one call shape (a POST that
# returns something with .status_code and .text) to intercept metrics, logs
# and annotations alike.

def _post(url: str, *, json_body: dict, headers: dict, auth: tuple[str, str] | None = None):
    return httpx.post(url, json=json_body, headers=headers, auth=auth, timeout=30.0)

def _check(resp) -> None:
    if resp.status_code >= 400:
        raise RuntimeError(f"telemetry push failed: HTTP {resp.status_code}: {resp.text[:500]}")

def _data_point(value: float, unix_seconds: float, attributes: dict[str, str]) -> dict:
    """One OTLP gauge dataPoint, exactly the shape verified working on
    2026-08-23: asDouble, timeUnixNano (string -- protobuf JSON's canonical
    mapping for a 64-bit int, and precision-safe for a nanosecond epoch
    timestamp that would overflow a JS/JSON double), attributes as a
    key/stringValue array."""
    return {
        "asDouble": float(value),
        "timeUnixNano": str(int(unix_seconds * 1_000_000_000)),
        "attributes": [
            {"key": k, "value": {"stringValue": str(v)}} for k, v in attributes.items()
        ],
    }

def _otlp_push(metrics: dict[str, list[dict]]) -> None:
    """POST one OTLP JSON payload carrying every (metric_name -> dataPoints)
    pair given, as ONE HTTP request (one resourceMetrics/scopeMetrics block,
    one metrics entry per name) -- this is what lets push_timeline batch an
    entire run's customs_risk samples into a single push, and lets
    push_status combine customs_market_status + customs_blocking into one
    call too."""
    url = settings.otlp_url.rstrip("/") + "/v1/metrics"
    payload = {
        "resourceMetrics": [{
            "resource": {"attributes": []},
            "scopeMetrics": [{
                "scope": {"name": "customs"},
                "metrics": [
                    {"name": name, "gauge": {"dataPoints": points}}
                    for name, points in metrics.items()
                ],
            }],
        }],
    }
    resp = _post(
        url,
        json_body=payload,
        headers={"Content-Type": "application/json"},
        auth=(settings.grafana_stack_id, settings.grafana_cloud_token),
    )
    _check(resp)

def _loki_push(streams: list[dict]) -> None:
    resp = _post(
        settings.loki_push_url,
        json_body={"streams": streams},
        headers={"Content-Type": "application/json"},
        auth=(settings.loki_user, settings.grafana_cloud_token),
    )
    _check(resp)

def _annotation_post(body: dict) -> None:
    url = settings.grafana_url.rstrip("/") + "/api/annotations"
    resp = _post(
        url,
        json_body=body,
        headers={
            "Authorization": f"Bearer {settings.grafana_sa_token}",
            "Content-Type": "application/json",
        },
    )
    _check(resp)

# --- public API ---

def push_timeline(
    run: RunRecord, findings: list[Finding], duration: float, store: Store | None = None
) -> None:
    """Write the mapped-clock risk timeline: one customs_risk sample per
    whole video second per market present in `findings`.

    A market with zero findings gets no customs_risk series at all (this is
    the literal task-11-brief.md contract -- "per market present in
    findings" -- not an oversight: a fully clean market has nothing to draw
    on the heatmap; its clearance status still reaches Grafana via
    push_status's current-clock customs_market_status, independently).

    For each (market, second n) pair the value is the max severity among
    findings whose [t_start, t_end) span overlaps the one-second bucket
    [n, n+1) -- i.e. `f.t_start < n + 1 and f.t_end > n`, so a 12.4..14.1
    finding covers seconds 12, 13 and 14. 0 when nothing covers that
    second. dimension is the covering finding's rule's dimension (looked
    up via the market pack, see _dimension_for), or "none" for a
    zero-value second. Ties in max severity are broken by `findings`'
    input order -- the first qualifying finding wins the value and the
    dimension label both.

    t0 = push_time - duration (see module docstring for why): every sample
    this call writes therefore lands at or before push_time, never in the
    future. t0 is stored on the run record via store.set_run_t0 so every
    later push_log/annotate/annotate_resolution call for this run maps onto
    the exact same clock (design spec section 9: "so panels and drill-downs
    agree").

    Every sample for every market goes out as ONE OTLP HTTP request
    (task-11-brief.md: "batched in ONE OTLP payload").
    """
    store = store or _store()
    push_time = time.time()
    t0 = push_time - duration
    store.set_run_t0(run.id, t0)

    asset = _asset_label(run)
    n_seconds = math.ceil(duration)
    markets = sorted({f.market for f in findings})

    data_points = []
    for market in markets:
        market_findings = [f for f in findings if f.market == market]
        for n in range(n_seconds):
            covering = [f for f in market_findings if f.t_start < n + 1 and f.t_end > n]
            if covering:
                best = max(covering, key=lambda f: f.severity)
                value = float(best.severity)
                dimension = _dimension_for(best.market, best.rule_id)
            else:
                value = 0.0
                dimension = "none"
            data_points.append(_data_point(
                value, t0 + n, {"asset": asset, "market": market, "dimension": dimension}
            ))

    if data_points:
        _otlp_push({"customs_risk": data_points})

def push_status(
    run: RunRecord, market: str, clearance: str, findings: list[Finding]
) -> None:
    """Write the current-clock alerting series for one market: ALERTING
    series, stamped at real time.time(), never the mapped clock.

    Deviates from task-11-brief.md's literal 4th parameter name
    (`blocking_count`, a bare int): customs_blocking needs each blocking
    finding's own rule_id and severity, which a scalar count cannot supply,
    so this takes the findings list instead -- exactly the "accept a
    findings list param if cleaner; document" allowance the brief gives.
    A caller that wants blocking_count for a UI tile can take len() of the
    filtered set itself; nothing here needs to hand it back.

    customs_market_status{asset,market} = _CLEARANCE_CODE[clearance]
    (0 cleared, 1 at_risk, 2 blocked -- design spec section 8's clearance()
    bands, not recomputed here, just encoded).

    customs_blocking{asset,market,rule_id} = severity, one sample per
    finding in `findings` that is currently actually blocking clearance:
    market-matched, klass == "legal" (only a legal finding can ever cause
    "blocked" in adjudicate.clearance() -- a policy finding causes at_risk,
    a different metric), sourced, status == "open", severity >=
    CLEARANCE_SEVERITY_THRESHOLD (the same 70 clearance() itself uses,
    imported rather than re-pinned as a second magic number). A finding
    that stops qualifying (resolved, or no longer passed in) simply gets no
    new sample on the next push_status call; per design spec section 9
    ("the metric drops and Grafana resolves the alert on its own") that
    series goes stale and ages out of the alert's evaluation window --
    nothing here ever deletes or zeroes a Prometheus sample, because you
    can't.

    Both metrics go out as one OTLP HTTP request.
    """
    asset = _asset_label(run)
    now = time.time()

    status_points = [_data_point(
        _CLEARANCE_CODE[clearance], now, {"asset": asset, "market": market}
    )]

    blocking = [
        f for f in findings
        if f.market == market
        and f.klass == "legal"
        and f.sourced
        and f.status == "open"
        and f.severity >= CLEARANCE_SEVERITY_THRESHOLD
    ]
    blocking_points = [
        _data_point(f.severity, now, {"asset": asset, "market": market, "rule_id": f.rule_id})
        for f in blocking
    ]

    metrics = {"customs_market_status": status_points}
    if blocking_points:
        metrics["customs_blocking"] = blocking_points
    _otlp_push(metrics)

def push_log(run: RunRecord, finding: Finding) -> None:
    """Write one Loki line for `finding`. Labels {app="customs", asset,
    market, klass, rule_id}; body is the finding's own to_json(), so every
    field (rationale, citation, severity, t_start/t_end, ...) is queryable
    from the log line itself without a join back to sqlite.

    Stamped on the MAPPED clock (t0 + finding.t_start), not current-clock:
    design spec section 9, "Loki receives finding detail on the same mapped
    clock" -- so a Loki drill-down from a customs_risk heatmap cell lands on
    a log line from the same moment on the same axis.
    """
    ts_ns = str(int(_mapped_unix_seconds(run, finding.t_start) * 1_000_000_000))
    stream = {
        "stream": {
            "app": "customs",
            "asset": _asset_label(run),
            "market": finding.market,
            "klass": finding.klass,
            "rule_id": finding.rule_id,
        },
        "values": [[ts_ns, json.dumps(finding.to_json())]],
    }
    _loki_push([stream])

# Module-level stage-error counters, keyed by (run.id, stage). task-11-brief.md
# offers a choice ("keep a module-level or store-derived count; document"):
# store-derived was not chosen because push_stage_error's given signature
# (run, stage) carries no store param to query with, and adding one silently
# would mean two different call sites for the same "increment" idea (this
# one and push_timeline/annotate_resolution's explicit optional `store`)
# behaving inconsistently for no real benefit. Keyed by run.id rather than
# by the pushed `asset` label alone so two different runs of a
# same-named asset never share (and silently cross-pollinate) a counter;
# each run's stage-error count starts fresh at 1, which is what "how many
# stage errors has THIS run hit for this stage" should mean on the Overview
# page. In-memory only: a fresh process starts every run's counters at
# zero, which reads as a legitimate counter reset to any PromQL
# rate()/increase() over it, the same as a real restarting process.
_stage_error_counts: dict[tuple[str, str], int] = {}

def push_stage_error(run: RunRecord, stage: str) -> None:
    """Increment and push the current-clock customs_stage_error{asset,stage}
    gauge (a hand-rolled counter, not an OTLP Sum -- see the module-level
    dict above). Design spec section 14: "a stage error does not fail the
    run. It is published to Grafana ... and rendered on the Overview page."
    """
    key = (run.id, stage)
    _stage_error_counts[key] = _stage_error_counts.get(key, 0) + 1
    asset = _asset_label(run)
    _otlp_push({
        "customs_stage_error": [
            _data_point(_stage_error_counts[key], time.time(), {"asset": asset, "stage": stage})
        ],
    })

def annotate(run: RunRecord, finding: Finding) -> None:
    """Create a Grafana annotation marking `finding` on the run's mapped
    clock (design spec section 9: "Every finding is also written as a
    Grafana annotation on the run's timeline"). time/timeEnd are the
    finding's own t_start/t_end mapped through this run's t0, so the
    annotation lines up with the same finding's customs_risk heatmap cells
    and push_log Loki line. tags = ["customs", asset, market, rule_id] --
    the same {asset, market, rule_id} triple an alert payload carries, so a
    human (or the Remediator, per design spec section 9) can go from either
    one to the other.
    """
    body = {
        "time": int(_mapped_unix_seconds(run, finding.t_start) * 1000),
        "timeEnd": int(_mapped_unix_seconds(run, finding.t_end) * 1000),
        "tags": ["customs", _asset_label(run), finding.market, finding.rule_id],
        "text": (
            f"{finding.rule_id} ({finding.klass}, severity {finding.severity}): "
            f"{finding.rationale}"
        ),
    }
    _annotation_post(body)

def annotate_resolution(run: RunRecord, change: ChangeRecord, store: Store | None = None) -> None:
    """Create the resolving Grafana annotation for `change`, tagged the same
    as annotate()'s original finding annotation plus "resolved" (design
    spec section 9: "every remediation writes the resolving annotation").

    ChangeRecord (schema.py) carries only finding_id, not market/rule_id/
    t_start/t_end -- the associated Finding is looked up via
    store.findings(change.run_id), the one Store method that already
    exists for this (no new Store method added for Task 11; store.py is
    out of this task's file list). `store` is optional for the same reason
    push_timeline's is: the brief-literal 2-arg call form still works via
    the lazy singleton default.
    """
    store = store or _store()
    finding = next(
        (f for f in store.findings(change.run_id) if f.id == change.finding_id), None
    )
    if finding is None:
        raise ValueError(
            f"annotate_resolution: finding {change.finding_id!r} not found in run "
            f"{change.run_id!r}"
        )
    body = {
        "time": int(_mapped_unix_seconds(run, finding.t_start) * 1000),
        "timeEnd": int(_mapped_unix_seconds(run, finding.t_end) * 1000),
        "tags": ["customs", _asset_label(run), finding.market, finding.rule_id, "resolved"],
        "text": f"resolved {finding.rule_id} via {change.method}: {change.description}",
    }
    _annotation_post(body)
