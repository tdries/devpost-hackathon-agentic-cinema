"""Live probe: how far can an OTLP metric sample be back- or forward-dated
relative to push time before Grafana Cloud's Mimir OTLP gateway rejects it?

Task 11 (telemetry.py) needs the mapped-clock trick to draw the risk
timeline: video second n is written at wall-clock t0 + n so Prometheus never
sees an out-of-order sample. The exact accepted window (how far Mimir lets a
sample be backdated, and how far into the future) is a property of this
specific Grafana Cloud stack's ingester configuration, not something to
assume from generic Prometheus defaults -- so this script pushes real
samples at known offsets and queries them back, live, before telemetry.py's
t0 strategy gets written.

Pushes customs_backdate_probe gauge samples at five offsets from one
push_time: now-2m, now-5m, now-10m, now+30s, now+90s, each with a distinct
probe_offset label. Sleeps 20s (ingestion + indexing latency), then queries
each one back through the Grafana datasource proxy with an explicit
`time=` pinned to that sample's own timestamp (never relying on "now" plus
the instant-query default 5m lookback, which would falsely read the
now-5m/now-10m samples as missing purely due to query timing, independent
of whether Mimir actually accepted them). Prints a per-offset accepted/
rejected table.

Run once, by hand:
    source .venv/bin/activate && python scripts/probe_backdate.py
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import httpx

from customs.config import settings

# (label, offset_seconds_from_push_time)
OFFSETS = [
    ("past_2m", -120),
    ("past_5m", -300),
    ("past_10m", -600),
    ("future_30s", 30),
    ("future_90s", 90),
]

SLEEP_SECONDS = 20


def push_probe_sample(label: str, sample_time: float) -> tuple[int, str]:
    """POST one OTLP JSON gauge sample for customs_backdate_probe. Returns (status_code, response body snippet)."""
    payload = {
        "resourceMetrics": [{
            "resource": {"attributes": []},
            "scopeMetrics": [{
                "scope": {"name": "customs-probe"},
                "metrics": [{
                    "name": "customs_backdate_probe",
                    "gauge": {
                        "dataPoints": [{
                            "asDouble": float(sample_time),
                            "timeUnixNano": str(int(sample_time * 1_000_000_000)),
                            "attributes": [
                                {"key": "probe_offset", "value": {"stringValue": label}},
                            ],
                        }],
                    },
                }],
            }],
        }],
    }
    url = settings.otlp_url.rstrip("/") + "/v1/metrics"
    resp = httpx.post(
        url,
        json=payload,
        auth=(settings.grafana_stack_id, settings.grafana_cloud_token),
        headers={"Content-Type": "application/json"},
        timeout=30.0,
    )
    return resp.status_code, resp.text[:300].replace("\n", " ")


def query_probe_sample(label: str, sample_time: float) -> tuple[bool, str]:
    """Instant-query the datasource proxy for this sample, evaluated exactly at
    its own timestamp so query timing can never masquerade as a rejection."""
    query = f'customs_backdate_probe{{probe_offset="{label}"}}'
    url = settings.grafana_url.rstrip("/") + "/api/datasources/proxy/uid/grafanacloud-prom/api/v1/query"
    resp = httpx.get(
        url,
        params={"query": query, "time": f"{sample_time:.3f}"},
        headers={"Authorization": f"Bearer {settings.grafana_sa_token}"},
        timeout=30.0,
    )
    if resp.status_code != 200:
        return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
    data = resp.json()
    result = data.get("data", {}).get("result", [])
    if not result:
        return False, "empty result"
    value = result[0].get("value", [None, None])[1]
    return True, f"value={value}"


def main() -> list[tuple[str, int, int, bool, str, str]]:
    push_time = time.time()
    print(f"push_time = {push_time:.3f} ({time.strftime('%Y-%m-%d %H:%M:%S %Z', time.localtime(push_time))})")

    sample_times = {label: push_time + offset for label, offset in OFFSETS}

    print("\n--- pushing ---")
    push_status: dict[str, int] = {}
    for label, offset in OFFSETS:
        status, body = push_probe_sample(label, sample_times[label])
        push_status[label] = status
        print(f"push {label:12} offset={offset:+5d}s -> HTTP {status}")
        if status >= 400:
            print(f"    body: {body}")

    print(f"\nsleeping {SLEEP_SECONDS}s for ingestion before query-back...")
    time.sleep(SLEEP_SECONDS)

    print("\n--- querying back (time= pinned to each sample's own timestamp) ---")
    rows = []
    for label, offset in OFFSETS:
        status = push_status[label]
        push_ok = status < 400
        if push_ok:
            query_ok, detail = query_probe_sample(label, sample_times[label])
        else:
            query_ok, detail = False, "skipped (push rejected)"
        accepted = push_ok and query_ok
        verdict = "ACCEPTED" if accepted else "REJECTED"
        rows.append((label, offset, status, query_ok, detail, verdict))

    header = f"{'offset':11} {'push_http':10} {'query_ok':9} {'detail':30} verdict"
    print(header)
    print("-" * len(header))
    for label, offset, status, query_ok, detail, verdict in rows:
        print(f"{offset:+5d}s     {status:<10} {str(query_ok):9} {detail:30} {verdict}")

    print("\n--- accepted/rejected summary ---")
    for label, offset, status, query_ok, detail, verdict in rows:
        print(f"{label:12} ({offset:+d}s): {verdict}")

    return rows


if __name__ == "__main__":
    main()
