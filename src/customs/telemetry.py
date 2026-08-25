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
satisfies the FIRST branch of the rule, so per controller dispatch (task 11)'s
"implement whichever branch the probe selects; do not implement the others"
this file implements ONLY:

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
# (controller dispatch (task 11): "httpx mocked via monkeypatching a
# module-level _post function" -- this exact phrase is not in task-11-brief.md
# itself, which only says "httpx transport mocked"). _otlp_push, _loki_push
# and _annotation_post all funnel through this, so a test double only ever
# has to fake one call shape (a POST that returns something with
# .status_code and .text) to intercept metrics, logs and annotations alike.

def _post(url: str, *, json_body: dict, headers: dict, auth: tuple[str, str] | None = None):
    return httpx.post(url, json=json_body, headers=headers, auth=auth, timeout=30.0)

def _get(url: str, *, params: dict, headers: dict):
    """The read seam, added for annotation dedup. Kept separate from _post so
    a test double can answer "what is already there" without also having to
    fake every write."""
    return httpx.get(url, params=params, headers=headers, timeout=30.0)

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

# --- annotations: rate limiting and duplicates (both hit live in Task 12) ---
#
# Task 12 pushed 14 findings' annotations in a tight loop and Grafana answered
# HTTP 429 Too Many Requests partway through. _check turned that into a
# RuntimeError that aborted the caller, and the partial first batch left the
# window holding 25 annotations for 14 findings. Two fixes, both here:
#
#   1. _annotation_post retries a 429, honouring Retry-After when Grafana
#      sends one and backing off exponentially when it does not, up to
#      _ANNOTATE_MAX_ATTEMPTS tries. Nothing else about _check changes: a 4xx
#      that is not a 429, and a 5xx, still raise on the first answer.
#   2. annotate() asks Grafana what is already on this run's clock and skips
#      any annotation whose (tags, time, timeEnd) triple is already there.
#
# Dedup is deliberately annotate()-only. annotate_resolution writes one
# annotation per ChangeRecord, so it cannot loop, and a second remediation of
# the same finding is a real second event that must not be swallowed. It
# still gets the 429 backoff, since both share _annotation_post.
_ANNOTATE_MAX_ATTEMPTS = 5
_ANNOTATE_BACKOFF_BASE_SECONDS = 1.0
# How far past t0 to look for this run's existing annotations. Comfortably
# wider than the spec's 120s cap on ad duration, and narrow enough that the
# query stays bounded on a stack with months of runs on it.
_ANNOTATION_WINDOW_SECONDS = 3600.0
_ANNOTATION_QUERY_LIMIT = 500

def _retry_after_seconds(resp) -> float | None:
    """Grafana's Retry-After in seconds, or None if it did not send a usable
    one. Only the delta-seconds form is honoured; the HTTP-date form falls
    through to the exponential backoff rather than being half-parsed."""
    raw = resp.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return None

def _annotation_post(body: dict) -> None:
    url = settings.grafana_url.rstrip("/") + "/api/annotations"
    headers = {
        "Authorization": f"Bearer {settings.grafana_sa_token}",
        "Content-Type": "application/json",
    }
    for attempt in range(_ANNOTATE_MAX_ATTEMPTS):
        resp = _post(url, json_body=body, headers=headers)
        if resp.status_code != 429:
            _check(resp)
            return
        if attempt == _ANNOTATE_MAX_ATTEMPTS - 1:
            break
        wait = _retry_after_seconds(resp)
        if wait is None:
            wait = _ANNOTATE_BACKOFF_BASE_SECONDS * (2 ** attempt)
        time.sleep(wait)
    _check(resp)  # every attempt was a 429: raise carrying the last one

def _annotation_key(tags, time_ms, time_end_ms) -> tuple:
    """The identity of one annotation, for dedup: its tag set plus its exact
    span on the run's mapped clock. Tags are sorted because Grafana does not
    promise to give them back in the order they were written, and the span is
    what separates two runs of the same asset (each run picks its own t0, so
    the same finding lands at a different millisecond every time).

    The finding id is one of those tags (see annotate), which is what stops
    two distinct findings that share a market, a rule and a span from
    collapsing into one key. An annotation written before the id tag existed
    carries four tags rather than five and so never matches a new key: it
    cannot suppress a write, which is the safe direction to fail."""
    return (tuple(sorted(str(t) for t in tags)), int(time_ms), int(time_end_ms))

def existing_annotation_keys(run: RunRecord) -> set[tuple]:
    """Every annotation already on this run's mapped clock, as dedup keys.

    One query per run, not one per finding: the per-finding loop is exactly
    what got rate limited. Pass the result to annotate() as `existing` and a
    whole run's annotations cost one GET plus one POST per new finding.
    """
    resp = _get(
        settings.grafana_url.rstrip("/") + "/api/annotations",
        params={
            "tags": ["customs", _asset_label(run)],
            "from": int(_mapped_unix_seconds(run, 0.0) * 1000),
            "to": int(_mapped_unix_seconds(run, _ANNOTATION_WINDOW_SECONDS) * 1000),
            "limit": _ANNOTATION_QUERY_LIMIT,
        },
        headers={"Authorization": f"Bearer {settings.grafana_sa_token}"},
    )
    _check(resp)
    return {
        _annotation_key(item.get("tags") or [], item.get("time", 0), item.get("timeEnd", 0))
        for item in (resp.json() or [])
    }

# --- public API ---

def push_timeline(
    run: RunRecord, findings: list[Finding], duration: float, store: Store | None = None
) -> None:
    """Write the mapped-clock risk timeline: one customs_risk sample per
    whole video second per market in `run.markets`.

    Iterates run.markets, not the set of markets appearing in `findings`.
    Fixed post-review (2026-08-23): the original version iterated only
    `sorted({f.market for f in findings})`, so a market that cleared with
    zero findings got no customs_risk series at all -- on the Task 12
    heatmap that renders as a missing row, indistinguishable from a market
    that was never evaluated, and an all-clean run pushed nothing at all.
    A clean market now gets a full run of all-zero, dimension="none"
    samples, one per second, same as any other market; its clearance
    status also reaches Grafana separately via push_status's current-clock
    customs_market_status.

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
    (controller dispatch (task 11): "batched in ONE OTLP payload" -- this
    exact phrase is not in task-11-brief.md, which does not specify
    batching for push_timeline at all).

    Assumes one push_timeline call per run: t0 is recomputed from
    time.time() on every call and overwrites the run's previously stored
    t0, so a second call for the same run would move future push_log/
    annotate lookups onto a new mapped clock without also re-mapping any
    customs_risk samples the first call already wrote -- those would be
    stranded on the old, now-orphaned t0. Nothing in this task calls it
    more than once per run.
    """
    store = store or _store()
    push_time = time.time()
    t0 = push_time - duration
    store.set_run_t0(run.id, t0)

    asset = _asset_label(run)
    n_seconds = math.ceil(duration)
    markets = sorted(run.markets)

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

def extend_timeline(run: RunRecord, findings: list[Finding], duration: float,
                    markets: list[str]) -> None:
    """customs_risk for markets judged after the run's clock was mapped.

    push_timeline may not be called twice: it re-picks t0 from time.time()
    and overwrites the stored one, stranding every sample the first call
    wrote on an orphaned clock. A market added later still needs its row on
    the heatmap and its share of the dimension panels, so this writes the
    same per-second samples for just those markets onto the EXISTING t0,
    and never touches it.
    """
    if run.t0 is None:
        raise ValueError(f"run {run.id!r} has no t0 -- push_timeline first")
    asset = _asset_label(run)
    data_points = []
    for market in sorted(markets):
        market_findings = [f for f in findings if f.market == market]
        for n in range(math.ceil(duration)):
            covering = [f for f in market_findings if f.t_start < n + 1 and f.t_end > n]
            if covering:
                best = max(covering, key=lambda f: f.severity)
                value, dimension = float(best.severity), _dimension_for(best.market, best.rule_id)
            else:
                value, dimension = 0.0, "none"
            data_points.append(_data_point(
                value, run.t0 + n, {"asset": asset, "market": market, "dimension": dimension}))
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
    findings list param if cleaner; document" allowance controller
    dispatch (task 11) gives (that specific allowance is not written in
    task-11-brief.md itself, which only states the literal blocking_count
    signature). A caller that wants blocking_count for a UI tile can take
    len() of the filtered set itself; nothing here needs to hand it back.

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
    market, klass, rule_id, dimension}; body is the finding's own to_json(), so every
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
            "dimension": _dimension_for(finding.market, finding.rule_id),
            # so {kind="finding"} and {kind="observation"} can be asked for
            # separately; without it every query catches both.
            "kind": "finding",
        },
        "values": [[ts_ns, json.dumps(finding.to_json())]],
    }
    _loki_push([stream])

# Module-level stage-error counters, keyed by (run.id, stage). controller
# dispatch (task 11) offers a choice ("keep a module-level or store-derived
# count; document") -- that phrase is not in task-11-brief.md itself, which
# only says push_stage_error increments "current-clock counter
# customs_stage_error{asset, stage}" with no mention of how the count is
# kept. store-derived was not chosen because push_stage_error's given signature
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

# --- observations: the half of the picture Grafana could not see --------
#
# A finding only exists where a market objected. Pushing findings alone
# therefore records every complaint and none of the context: what the
# analyst actually saw, how sure it was, and above all everything it saw
# that NOBODY objected to. That negative space is the more interesting
# half -- it is what tells an operator which markets are permissive and
# which details are universally safe -- and it was dying in SQLite.
#
# Labels are chosen for cardinality, not convenience. asset (about a
# dozen) x dimension (exactly 18) x flagged (2) is a few hundred streams
# and will stay that way. shot_id, the statement, the confidence and the
# box go in the body, where they are queryable with | json and cost
# nothing. `kind` separates these lines from finding lines so a query can
# ask for one without catching the other.
_OBS_LABEL_KIND = "observation"


def push_observation(run: RunRecord, obs, findings: list[Finding] | None = None) -> None:
    """One Loki line per observation, on the run's mapped clock.

    `findings` are the findings hung off this observation, so the line can
    say how many markets objected without a join at query time.
    """
    hits = list(findings or [])
    markets = sorted({f.market for f in hits})
    body = {
        "run_id": run.id,
        "observation_id": obs.id,
        "shot_id": obs.shot_id,
        "t_start": obs.t_start,
        "t_end": obs.t_end,
        "dimension": obs.dimension,
        "statement": obs.statement,
        "confidence": obs.confidence,
        "has_frame": bool(obs.evidence_frame),
        "has_box": bool(getattr(obs, "box", None)),
        "findings": len(hits),
        "markets": markets,
        "max_severity": max((f.severity for f in hits), default=0),
        "rules": sorted({f.rule_id for f in hits}),
    }
    stream = {
        "stream": {
            "app": "customs",
            "kind": _OBS_LABEL_KIND,
            "asset": _asset_label(run),
            "dimension": obs.dimension or "none",
            "flagged": "yes" if hits else "no",
        },
        "values": [[str(int(_mapped_unix_seconds(run, obs.t_start) * 1_000_000_000)),
                    json.dumps(body)]],
    }
    _loki_push([stream])


def push_observations(run: RunRecord, observations, findings: list[Finding]) -> int:
    """Every observation of a run, in one request rather than N.

    Loki takes many streams per push, and a run has dozens of
    observations; pushing them one at a time is how the annotation loop
    got rate limited in Task 12.
    """
    by_obs: dict[str, list] = {}
    for finding in findings or []:
        by_obs.setdefault(finding.observation_id, []).append(finding)
    streams = []
    for obs in observations or []:
        hits = by_obs.get(obs.id, [])
        body = {
            "run_id": run.id, "observation_id": obs.id, "shot_id": obs.shot_id,
            "t_start": obs.t_start, "t_end": obs.t_end,
            "dimension": obs.dimension, "statement": obs.statement,
            "confidence": obs.confidence,
            "has_frame": bool(obs.evidence_frame),
            "has_box": bool(getattr(obs, "box", None)),
            "findings": len(hits),
            "markets": sorted({f.market for f in hits}),
            "max_severity": max((f.severity for f in hits), default=0),
            "rules": sorted({f.rule_id for f in hits}),
        }
        streams.append({
            "stream": {
                "app": "customs", "kind": _OBS_LABEL_KIND,
                "asset": _asset_label(run),
                "dimension": obs.dimension or "none",
                "flagged": "yes" if hits else "no",
            },
            "values": [[str(int(_mapped_unix_seconds(run, obs.t_start) * 1_000_000_000)),
                        json.dumps(body)]],
        })
    if streams:
        _loki_push(streams)
    return len(streams)


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

def annotate(run: RunRecord, finding: Finding, existing: set[tuple] | None = None) -> bool:
    """Create a Grafana annotation marking `finding` on the run's mapped
    clock (design spec section 9: "Every finding is also written as a
    Grafana annotation on the run's timeline"). time/timeEnd are the
    finding's own t_start/t_end mapped through this run's t0, so the
    annotation lines up with the same finding's customs_risk heatmap cells
    and push_log Loki line. tags = ["customs", asset, market, rule_id,
    finding_id] -- the {asset, market, rule_id} triple is the same one an
    alert payload carries, so a human (or the Remediator, per design spec
    section 9) can go from either one to the other.

    finding.id is a tag as well, and it is what makes the dedup key below
    identify a *finding* rather than a rule-in-a-time-span. Two findings can
    legitimately share market, rule_id and span -- the same shot can trigger
    one rule from two different observations -- and both of them are real
    markers that must both be drawn. Without the id tag their dedup keys
    collide and the second one is silently dropped.

    Skips an annotation Grafana already holds, and returns whether it wrote
    one. `existing` is the dedup set from existing_annotation_keys(); pass
    the same set to every call in a loop and it costs one query for the whole
    run and also stops the loop duplicating within itself. Left out, each
    call queries for itself, which keeps the brief-literal 2-argument form
    working and correct.
    """
    body = {
        "time": int(_mapped_unix_seconds(run, finding.t_start) * 1000),
        "timeEnd": int(_mapped_unix_seconds(run, finding.t_end) * 1000),
        "tags": ["customs", _asset_label(run), finding.market, finding.rule_id, finding.id],
        "text": (
            f"{finding.rule_id} ({finding.klass}, severity {finding.severity}): "
            f"{finding.rationale}"
        ),
    }
    key = _annotation_key(body["tags"], body["time"], body["timeEnd"])
    if existing is None:
        existing = existing_annotation_keys(run)
    if key in existing:
        return False
    _annotation_post(body)
    existing.add(key)
    return True

def annotate_resolution(run: RunRecord, change: ChangeRecord, store: Store | None = None) -> None:
    """Create the resolving Grafana annotation for `change`, tagged the same
    as annotate()'s original finding annotation (finding.id included) plus
    "resolved" (design spec section 9: "every remediation writes the
    resolving annotation"). Keeping the tag sets aligned is what lets a
    reader pair a finding marker with the marker that resolved it.

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
        "tags": [
            "customs", _asset_label(run), finding.market, finding.rule_id,
            finding.id, "resolved",
        ],
        "text": f"resolved {finding.rule_id} via {change.method}: {change.description}",
    }
    _annotation_post(body)
