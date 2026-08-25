"""Push every observation already in the store to Loki.

Observations were only ever written to SQLite, so Grafana has no record of
what the analyst saw -- only of what a market objected to. This walks the
history once so the dashboards and the MCP agent start with the whole
picture rather than with tonight's runs.

Idempotent in practice: Loki drops an exact duplicate of (stream, ts,
line), and re-running produces byte-identical lines.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from customs import telemetry
from customs.config import settings
from customs.store import Store


def main(db_path: str) -> int:
    store = Store(db_path)
    total = skipped = 0
    for run in store.recent_runs(500):
        observations = store.observations(run.id)
        if not observations:
            continue
        if run.t0 is None:
            print(f"  {run.id}: no mapped clock, skipped ({len(observations)} obs)")
            skipped += len(observations)
            continue
        findings = store.findings(run.id)
        try:
            n = telemetry.push_observations(run, observations, findings)
        except Exception as exc:  # noqa: BLE001 -- report and keep going
            print(f"  {run.id}: FAILED {exc!r}")
            continue
        flagged = len({f.observation_id for f in findings})
        print(f"  {run.id}: {n:3} observation(s), {flagged} flagged, "
              f"{n - flagged} that nobody objected to")
        total += n
    print(f"\npushed {total} observation(s); {skipped} had no clock to map onto")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else settings.db_path))
