"""Render what this system actually keeps in Loki and Mimir, as pictures.

For a Grafana reader who wants the receipts rather than the claim: the raw
log lines with their labels, the same lines drawn as a state timeline, and
the metric series on the film's own clock.

The trap these exist because of: a run's telemetry sits on the MAPPED
clock, where wall second t0+n is video second n. Rendering a dashboard
over "now-6h" shows an empty afternoon, which is exactly what the stock
findings dashboard renders as "No data". Every panel here is windowed to
the run.

    python scripts/make_store_shots.py --run run_xxx
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from customs.config import settings              # noqa: E402
from customs.grafana_ops import GrafanaOps, LOKI_UID, PROM_UID  # noqa: E402
from customs.store import Store                  # noqa: E402

OUT = ROOT / "docs" / "media"


def panel(pid, title, kind, source, expr, x, y, w, h, **opts):
    return {
        "id": pid, "type": kind, "title": title,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "datasource": {"type": "loki" if source == "loki" else "prometheus",
                       "uid": LOKI_UID if source == "loki" else PROM_UID},
        "targets": [{"refId": "A", "expr": expr,
                     **({"queryType": "range"} if source == "loki" else {})}],
        "options": opts or {},
    }


def dashboard(asset: str) -> dict:
    """Four panels: the lines, their shape, and the series beside them."""
    return {
        "uid": "customs-store-evidence",
        "title": "Customs: what is stored, and where",
        "tags": ["customs"],
        "timezone": "utc",
        "panels": [
            panel(1, f'Loki: kind="observation" — one line per keyframe, '
                     f'the analyst\'s own sentence in the body',
                  "logs", "loki",
                  f'{{app="customs", kind="observation", asset="{asset}"}}',
                  0, 0, 24, 9, showLabels=True, showTime=True, wrapLogMessage=True),
            panel(2, 'Loki: kind="finding" — the join, with its statute and citation',
                  "logs", "loki",
                  f'{{app="customs", kind="finding", asset="{asset}"}}',
                  0, 9, 24, 8, showLabels=True, showTime=True, wrapLogMessage=True),
            panel(3, "Loki: lines per dimension, from the labels alone",
                  "barchart", "loki",
                  f'sum by (dimension) (count_over_time({{app="customs", '
                  f'kind="observation", asset="{asset}"}}[$__range]))',
                  0, 17, 12, 8),
            panel(4, "Mimir: customs_risk, one sample per video second",
                  "state-timeline", "prometheus",
                  f'max by (market) (last_over_time(customs_risk'
                  f'{{asset=~"{asset}"}}[$__interval]))',
                  12, 17, 12, 8),
        ],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", default="run_804f7b687c72")
    ap.add_argument("--theme", default="light")
    args = ap.parse_args()

    store = Store(settings.db_path)
    run = store.get_run(args.run)
    if run is None:
        print(f"no such run in the local store: {args.run}")
        print("run this against the machine that holds the run, or pass --run")
        return 1
    asset = Path(run.asset_path).stem

    OUT.mkdir(parents=True, exist_ok=True)
    with GrafanaOps(settings) as ops:
        made = ops.create_adhoc_dashboard(dashboard(asset))
        print("dashboard:", made["url"])
        for name, pid, size in (("store-loki-observations", 1, (1400, 620)),
                                ("store-loki-findings", 2, (1400, 560)),
                                ("store-loki-by-dimension", 3, (900, 520)),
                                ("store-mimir-risk", 4, (900, 520))):
            png = ops.render_png(made["uid"], pid, run, width=size[0],
                                 height=size[1], theme=args.theme)
            dest = OUT / f"{name}.png"
            dest.write_bytes(png)
            print(f"  {dest.relative_to(ROOT)}  {len(png)//1024} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
