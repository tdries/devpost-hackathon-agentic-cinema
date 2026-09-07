"""Stamp the withheld-corpus matcher into every Loki query in the dashboards.

The console filters its own queries in code (config.withheld_matcher), but
the framed panels are Grafana's, and Grafana reads the JSON in
grafana/dashboards/ -- baked into the viewer image at build time and pushed
to Grafana Cloud at provisioning. A panel whose expression says only
`{app="customs", kind="finding"}` charts the whole tenant, which is how the
intelligence board came to show a row of studio cartoons and a Chanel spot
with a famous actor in it.

So the filter has to be IN the files, and this is what puts it there. Run it
after changing config.WITHHELD_ASSETS; test_grafana_map enforces that the
files and the constant agree, and its failure message names this script.

    python scripts/stamp_withheld_dashboards.py
    python scripts/stamp_withheld_dashboards.py --check   # exit 1 if stale

Per-run dashboards are stamped too and it costs nothing: their queries are
already pinned to one asset by a variable, so an extra "and not those
twenty-four" changes no picture.
"""
import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from customs.config import withheld_matcher  # noqa: E402

DASHBOARDS = ROOT / "grafana" / "dashboards"
# A stream selector mentioning this app, with whatever else it already
# carries. Nested braces do not occur in these expressions.
SELECTOR = re.compile(r'\{([^{}]*app\s*=\s*"customs"[^{}]*)\}')
# What a previous stamp left behind, so re-running is idempotent and a
# shortened list actually shrinks the regex instead of appending to it.
STAMPED = re.compile(r',\s*asset!~`\^\([^`]*\)\$`')


def stamp(expr: str, matcher: str) -> str:
    """The expression with exactly one current matcher per selector."""
    def once(match: re.Match) -> str:
        inner = STAMPED.sub("", match.group(1))
        return "{" + inner + matcher + "}"

    return SELECTOR.sub(once, expr)


def walk(path: Path, matcher: str) -> tuple[dict, int]:
    """The dashboard with every Loki expression stamped, and how many."""
    dashboard = json.loads(path.read_text())
    changed = 0
    for panel in dashboard.get("panels", []):
        for target in panel.get("targets", []):
            expr = target.get("expr")
            if not expr or (target.get("datasource") or {}).get("type") != "loki":
                continue
            stamped = stamp(expr, matcher)
            if stamped != expr:
                target["expr"] = stamped
                changed += 1
    return dashboard, changed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true",
                    help="report staleness instead of fixing it")
    args = ap.parse_args()

    matcher = withheld_matcher()
    stale = []
    for path in sorted(DASHBOARDS.glob("*.json")):
        dashboard, changed = walk(path, matcher)
        if not changed:
            continue
        stale.append(f"{path.name} ({changed} quer{'y' if changed == 1 else 'ies'})")
        if not args.check:
            path.write_text(json.dumps(dashboard, indent=2) + "\n")

    if args.check:
        if stale:
            print("stale, run scripts/stamp_withheld_dashboards.py: "
                  + ", ".join(stale))
            return 1
        print("every Loki query carries the current matcher")
        return 0
    print("stamped: " + (", ".join(stale) if stale else "nothing to change"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
