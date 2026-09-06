"""Re-push a finished run's telemetry, for a run whose publisher was throttled.

Grafana Cloud's free plan answers a share of OTLP writes with 429 and
err-mimir-tenant-max-request-rate regardless of the rate actually sent, and
until telemetry._post_retrying existed one rejection was final. A run could
complete perfectly -- findings judged, statutes cited, fixes verified -- and
have nothing at all in Loki or Mimir, because its whole publisher stage was
refused. run_3ea5230a429f is exactly that: sixteen findings in SQLite, zero
lines in Loki, and every Grafana panel on it reading "No data".

    python scripts/backfill_telemetry.py --run run_xxx
    python scripts/backfill_telemetry.py --all        # every run missing from Loki

The state is rebuilt from the store rather than from the run, because the
run is over: findings, observations, verdicts and clearances are all rows.
push_timeline re-picks t0, which is safe here and only here -- there are no
samples on the old clock to strand, since none were ever accepted.
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from customs import crew, telemetry                    # noqa: E402
from customs.config import settings                    # noqa: E402
from customs.media import probe_duration               # noqa: E402
from customs.pipeline import clearance                 # noqa: E402
from customs.store import Store                        # noqa: E402


def _state(store: Store, run) -> crew._RunState:
    findings = store.findings(run.id)
    by_market: dict[str, list] = {}
    for f in findings:
        by_market.setdefault(f.market, []).append(f)
    duration = 0.0
    try:
        duration = probe_duration(Path(run.asset_path))
    except Exception:  # noqa: BLE001 -- a missing master still has findings
        pass
    return crew._RunState(
        markets=list(run.markets), run_id=run.id, asset_path=run.asset_path,
        duration=duration or max((f.t_end for f in findings), default=1.0),
        findings=findings,
        judged=by_market,
        clearances={m: clearance(by_market.get(m, [])) for m in run.markets},
        verdicts=[],  # not kept in the store; the findings carry the yeses
    )


def _in_loki(asset: str) -> int:
    from customs.grafana_ops import GrafanaOps
    with GrafanaOps(settings) as ops:
        rows = ops.loki_instant(
            'sum(count_over_time({app="customs", asset="%s"}[30d]))' % asset)
    return int(rows[0]["value"]) if rows else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", default="")
    ap.add_argument("--all", action="store_true",
                    help="every finished run with no lines in Loki")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    store = Store(settings.db_path)
    if args.run:
        run = store.get_run(args.run)
        if run is None:
            print(f"no such run: {args.run}")
            return 1
        runs = [run]
    elif args.all:
        runs = [r for r in store.recent_runs(500) if r.status == "done"]
    else:
        print("pass --run or --all")
        return 1

    for run in runs:
        asset = Path(run.asset_path).stem or run.asset_path
        held = _in_loki(asset)
        findings = store.findings(run.id)
        if args.all and held:
            continue
        print(f"{run.id}  {asset}: {len(findings)} finding(s) in the store, "
              f"{held} line(s) in Loki")
        if args.dry_run or not findings:
            continue
        summary = crew._push_run_telemetry(
            store, _state(store, run),
            lambda agent, message: print(f"   {agent}: {message}"))
        print(f"   -> {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
