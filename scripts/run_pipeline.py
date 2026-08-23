"""Run the full Customs clearance pipeline end to end and print the result.

Usage:
    python scripts/run_pipeline.py ASSET MARKET [MARKET ...]
    python scripts/run_pipeline.py docs/samples/test_ad.mp4 FR SA US

Ingests the asset (shot detection, per-shot audio transcription), runs the
analyst over every shot, adjudicates each given market in turn (guard is
still the Task-9 identity placeholder), and prints a findings table plus a
per-market clearance line. See pipeline.run's docstring for the stage-error
handling that keeps one bad shot or market from taking down the whole run:
nothing here needs to catch anything, pipeline.run always returns a RunRecord
with status "done".
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from customs import pipeline  # noqa: E402
from customs.adjudicate import clearance  # noqa: E402
from customs.config import settings  # noqa: E402
from customs.store import Store  # noqa: E402

CITATION_URL_MAX = 60

def _truncate(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: limit - 3] + "..."

def _print_findings_table(findings) -> None:
    if not findings:
        print("(no findings)")
        return
    header = f"{'market':6} {'rule_id':14} {'severity':8} {'sourced':7} {'t_start':8} {'t_end':8} citation_ref"
    print(header)
    print("-" * len(header))
    for f in findings:
        print(
            f"{f.market:6} {f.rule_id:14} {f.severity:<8} {str(f.sourced):7} "
            f"{f.t_start:<8.2f} {f.t_end:<8.2f} {f.citation_ref}"
        )
        print(f"    citation_url: {_truncate(f.citation_url, CITATION_URL_MAX)}")
        print(f"    rationale:    {f.rationale}")

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run the Customs clearance pipeline end to end.")
    parser.add_argument("asset", help="path to the video asset, e.g. docs/samples/test_ad.mp4")
    parser.add_argument("markets", nargs="+", help="one or more market codes, e.g. FR SA US")
    parser.add_argument(
        "--workdir", default="runs/work",
        help="scratch directory for extracted frames/audio (default: runs/work)",
    )
    args = parser.parse_args(argv)

    store = Store(settings.db_path)
    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    run = pipeline.run(args.asset, args.markets, store, workdir)

    print(f"\nrun {run.id}: status={run.status} asset={run.asset_path} markets={run.markets}\n")

    all_findings = store.findings(run.id)
    _print_findings_table(all_findings)

    print()
    for market in run.markets:
        market_findings = [f for f in all_findings if f.market == market]
        status = clearance(market_findings)
        print(f"{market}: {status} ({len(market_findings)} finding(s))")

    return 0

if __name__ == "__main__":
    raise SystemExit(main())
