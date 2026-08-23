"""Build the whole Customs Grafana surface, live, in one pass.

Creates or updates the six dashboards, both alert rules and their 30s
evaluation group, the customs-webhook contact point and the notification policy
that routes team=customs alerts to it, then turns on public sharing for the two
pages the console embeds (overview and timeline) and prints their public URLs.

Idempotent: dashboard uids, alert rule uids and the contact point name are all
fixed strings, so running it twice updates in place rather than piling up
copies.

Prints the transport actually used for every operation (MCP tool name, or the
REST endpoint) so the run's own output is the record of which half of the
integration did what. Never prints a token.

    source .venv/bin/activate && python scripts/provision_grafana.py
    python scripts/provision_grafana.py --webhook https://example.run.app/alerts
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from customs.config import settings
from customs.grafana_ops import MAPPING, GrafanaOps

# Overwritten at deploy time with the real Cloud Run URL (task 13 owns the
# webhook receiver). A placeholder keeps the contact point and the routing tree
# real and inspectable before that service exists.
PLACEHOLDER_WEBHOOK = "https://customs.invalid/alerts/remediator"

# The two pages Launch Control embeds: the market tiles and the timecode
# heatmap. Public sharing is the primary embed path (design spec section 9);
# render_png is the fallback and needs no sharing.
#
# The flag is annotations_enabled, and it is per dashboard on purpose. A public
# dashboard serves its annotations from a public endpoint whether or not a
# panel on the page draws them, so the overview (which draws none) would
# otherwise publish every finding's text for nothing. The timeline draws the
# finding and remediation markers, so it keeps them.
PUBLIC_DASHBOARDS = {
    "customs-overview": False,
    "customs-timeline": True,
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--webhook", default=PLACEHOLDER_WEBHOOK,
                        help="alert webhook URL for the customs-webhook contact point")
    parser.add_argument("--no-public", action="store_true",
                        help="skip enabling public sharing")
    args = parser.parse_args()

    print(f"stack: {settings.grafana_url}")
    # with-statement, not a trailing ops.close(): anything that raises in the
    # middle of provisioning would otherwise leave the mcp-grafana subprocess
    # running after this script exits.
    with GrafanaOps(settings) as ops:
        return _provision(ops, args)


def _provision(ops, args) -> int:
    print(f"\nmcp-grafana tools discovered: {len(ops.mcp_tools)}")
    if ops.mcp_error:
        print("  !! MCP UNAVAILABLE, running HTTP only. The Publisher agent path "
              "needs MCP.")
        print(f"  !! {ops.mcp_error}")
    print("\ntransport per operation:")
    report = ops.transport_report()
    for op in MAPPING:
        print(f"  {op:28} {report[op]}")
        if MAPPING[op].note:
            print(f"  {'':28} note: {MAPPING[op].note}")

    print("\ndashboards:")
    dashboards = ops.ensure_dashboards()
    for name, uid in sorted(dashboards.items()):
        print(f"  {name:12} {uid:20} {settings.grafana_url.rstrip('/')}/d/{uid}")

    print("\nalert rules:")
    rules = ops.ensure_alert_rules()
    for title, uid in sorted(rules.items()):
        print(f"  {title:26} {uid}")

    print("\ncontact point and routing:")
    cp_uid = ops.ensure_contact_point(args.webhook)
    print(f"  customs-webhook -> {args.webhook}  (uid {cp_uid or 'existing'})")
    print("  notification policy: team=customs routes to customs-webhook")

    if not args.no_public:
        print("\npublic dashboards (no login: these pages are the judge facing "
              "surface and carry demo findings on purpose):")
        for uid, annotations in PUBLIC_DASHBOARDS.items():
            try:
                url = ops.enable_public(uid, annotations_enabled=annotations)
                print(f"  {uid:20} annotations={str(annotations):5} {url}")
            except Exception as exc:
                print(f"  {uid:20} FAILED: {exc}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
