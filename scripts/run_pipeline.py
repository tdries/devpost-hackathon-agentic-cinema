"""Run the full Customs clearance pipeline end to end and print the result.

Usage:
    python scripts/run_pipeline.py ASSET MARKET [MARKET ...]
    python scripts/run_pipeline.py docs/samples/test_ad.mp4 FR SA US

Ingests the asset (shot detection, per-shot audio transcription), runs the
analyst over every shot, adjudicates each given market in turn (including
the guard rule layer that blocks auto-remediation on protected-basis
findings), and prints any stage errors, a findings table, and a per-market
clearance line. See pipeline.run's
docstring for the stage-error handling that keeps one bad shot or market
from taking down the whole run: nothing here needs to catch anything for
that -- the three retry-wrapped stages never raise out of pipeline.run,
only an unreadable/corrupt asset failing shot detection itself would (not
caught here either, deliberately: there is nothing useful to print without
any shots). A market whose pack failed to load or whose judge() exhausted
its retries is never printed as clearance "cleared": pipeline.errored_markets
identifies it and this script prints "ERROR (stage failure, not evaluated)"
in its place, since for this product "we found nothing" and "we never
checked" must never look the same.
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

def _print_stage_errors(store: Store, run_id: str) -> None:
    # Visible on its own, ahead of the findings table: a clearance tool that
    # buries "we never checked this" in the same events table as everything
    # else is not meaningfully different from one that hides it entirely.
    stage_errors = [
        (agent, message)
        for (_id, _ts, agent, message) in store.events_since(run_id, 0)
        if "stage_error" in message
    ]
    if not stage_errors:
        return
    print(f"STAGE ERRORS ({len(stage_errors)}):")
    for agent, message in stage_errors:
        print(f"  [{agent}] {message}")
    print()

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

    _print_stage_errors(store, run.id)

    all_findings = store.findings(run.id)
    _print_findings_table(all_findings)

    errored = pipeline.errored_markets(store, run.id)
    print()
    for market in run.markets:
        if market in errored:
            print(f"{market}: ERROR (stage failure, not evaluated)")
            continue
        market_findings = [f for f in all_findings if f.market == market]
        status = clearance(market_findings)
        print(f"{market}: {status} ({len(market_findings)} finding(s))")

    return 0

if __name__ == "__main__":
    raise SystemExit(main())
