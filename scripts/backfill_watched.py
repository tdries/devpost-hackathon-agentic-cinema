"""Give older runs the 'watched for, not seen' rows their icons already had.

    .venv/bin/python scripts/backfill_watched.py [--apply]

The console draws six taxonomy icons beside every card's live Grafana panel,
greying the categories a run never saw. The panel draws a row per dimension
it finds lines for, so those greyed icons had no row and every row below the
gap lined up with the wrong category.

telemetry.push_watched now writes one kind="watched" line per unseen
dimension as a run finishes, which fixes runs from here on. This does the
same for the runs already in the archive, reading what they saw out of Loki
rather than out of a store this script cannot reach.

Dry by default: it prints what it would write and touches nothing.
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.request

from customs import packs
from customs.config import settings
from customs.grafana_ops import GrafanaOps
from customs.telemetry import _loki_push

DAYS = 60
# Loki refuses an entry older than its ingestion window, so a run from three
# weeks ago cannot be given lines on its own clock -- and lines written at
# today's clock would fall outside the window the panel is pinned to, which
# is no use to anyone. Those runs keep the rows they have.
INGEST_WINDOW_DAYS = 6.5
TAXONOMY = sorted(packs.taxonomy())
CONSOLE = "https://customs-app-akap4ao72a-ew.a.run.app"


def archive_runs() -> set[str]:
    """Only the runs the console actually lists.

    Loki also holds observation lines from the test suite, which pushes
    asset="clip" into the real log store -- eight hundred runs of it. Those
    have no card, no icons and nothing to line up, so they get nothing here.
    """
    with urllib.request.urlopen(f"{CONSOLE}/runs?all=1", timeout=30) as answer:
        page = answer.read().decode("utf-8", "replace")
    return set(re.findall(r"run_[a-f0-9]{12}", page))


def observed_by_run() -> dict[str, dict]:
    """What each run saw, and where on the clock it sat, from its own lines."""
    with GrafanaOps(settings) as ops:
        seen = ops.loki_lines('{app="customs", kind="observation"}',
                              days=DAYS, limit=5000)
        already = ops.loki_lines('{app="customs", kind="watched"}',
                                 days=DAYS, limit=5000)

    def body(row):
        try:
            return json.loads(row.get("line") or "{}")
        except (TypeError, ValueError):
            return {}

    done = {body(row).get("run_id") for row in already}
    runs: dict[str, dict] = {}
    for row in seen:
        payload = body(row)
        run_id = payload.get("run_id")
        labels = row.get("labels") or {}
        asset = labels.get("asset")
        dimension = labels.get("dimension") or payload.get("dimension")
        if not run_id or not asset:
            continue
        entry = runs.setdefault(run_id, {"asset": asset, "dims": set(), "ts": None})
        if dimension and dimension != "none":
            entry["dims"].add(dimension)
        ts = row.get("ts_ns")
        if ts:
            ns = int(ts)
            entry["ts"] = ns if entry["ts"] is None else min(entry["ts"], ns)
    if len(seen) >= 5000:
        print("WARNING: hit Loki's per-query cap; some runs may be missing here")
    live = archive_runs()
    return {r: v for r, v in runs.items()
            if r not in done and r in live and v["asset"] != "clip"}


def main() -> int:
    apply = "--apply" in sys.argv
    runs = observed_by_run()
    if not runs:
        print("nothing to backfill: every run already has its watched lines")
        return 0

    total = 0
    skipped = 0
    floor_ns = int((time.time() - INGEST_WINDOW_DAYS * 86400) * 1e9)
    for run_id, info in sorted(runs.items()):
        if (info["ts"] or 0) < floor_ns:
            skipped += 1
            continue
        missing = [d for d in TAXONOMY if d not in info["dims"]]
        if not missing:
            continue
        # inside the run's own window, which is what the panel is pinned to
        ts_ns = str(info["ts"] or int(time.time() * 1e9))
        streams = [{
            "stream": {"app": "customs", "kind": "watched",
                       "asset": info["asset"], "dimension": d, "flagged": "no"},
            "values": [[ts_ns, json.dumps(
                {"run_id": run_id, "dimension": d, "seen": False,
                 "max_severity": 0, "findings": 0,
                 "statement": "watched for, not seen in this run"})]],
        } for d in missing]
        print(f"{run_id}  {info['asset'][:34]:34} +{len(missing)} row(s)")
        total += len(streams)
        if apply:
            try:
                _loki_push(streams)
            except Exception as exc:  # noqa: BLE001 -- one run must not stop the rest
                print(f"    refused: {str(exc)[:110]}")

    print(f"\n{total} line(s) {'written' if apply else 'to write (dry run)'}"
          + (f", {skipped} run(s) too old for Loki to accept" if skipped else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
