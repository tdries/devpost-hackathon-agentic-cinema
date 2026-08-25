"""Can this Grafana stack be put in an iframe? Ask it, do not assume.

The answer has been got wrong twice in this repo, in both directions, because
it was read off the wrong response. It is cheap to settle and expensive to
guess, so this is the probe -- the same shape as probe_backdate.py, and its
output is pasted into spark.py where the drawing decision is made.

Run:  PYTHONPATH=src .venv/bin/python scripts/probe_framing.py

Result, dreamystairs2355.grafana.net, 2026-08-26:

    GET  public dashboard   200   csp frame-ancestors 'none'    x-frame-options ABSENT
    HEAD public dashboard   200   csp frame-ancestors ABSENT    x-frame-options deny
    GET  private dashboard  200   csp frame-ancestors 'none'    x-frame-options ABSENT
    HEAD private dashboard  302   csp frame-ancestors ABSENT    x-frame-options deny
    PUT  /api/admin/settings      403 {}

Which is to say: BOTH mechanisms are in play, but not on the same request.
A browser opening an iframe issues a GET, so the operative refusal is the CSP
directive `frame-ancestors 'none'`. A HEAD -- which is what you reach for when
you are checking headers by hand -- shows `x-frame-options: deny` instead and
no CSP at all, which is how an earlier note here concluded there was no
frame-ancestors directive.

The distinction matters because `frame-ancestors` CAN name permitted origins.
`'none'` is a decision rather than an absence, and on a self-hosted Grafana it
is exactly what `allow_embedding = true` relaxes. On a Cloud stack it is not
ours to relax: the admin settings API answers 403, as above. Public dashboards
-- which exist to be embedded -- are refused on the same terms.

So the console draws its own charts and renders Grafana's panels server-side.
That is not a workaround for something nobody switched on.
"""

import sys

sys.path.insert(0, "src")

from customs.config import settings  # noqa: E402
from customs.grafana_ops import GrafanaOps, _http  # noqa: E402


def _framing(headers) -> tuple[str, str]:
    """(csp frame-ancestors, x-frame-options) as they came back."""
    csp = headers.get("content-security-policy", "")
    ancestors = "ABSENT"
    for part in csp.split(";"):
        if "frame-ancestors" in part:
            ancestors = part.strip()
            break
    return ancestors, headers.get("x-frame-options", "ABSENT")


def main() -> int:
    with GrafanaOps(settings) as ops:
        targets = [
            ("public dashboard", settings.grafana_public_overview),
            ("private dashboard", f"{settings.grafana_url.rstrip('/')}/d/customs-lanes/customs"),
        ]
        for label, url in targets:
            for method in ("GET", "HEAD"):
                resp = _http(method, url, headers={}, timeout=30.0)
                ancestors, xfo = _framing(resp.headers)
                print(f"  {method:4s} {label:18s} {resp.status_code}  "
                      f"csp[{ancestors}]  x-frame-options[{xfo}]")

        # The one that would make the whole question moot, if it worked.
        resp = _http("PUT", ops._url("/api/admin/settings"),
                     headers=ops._headers({"Content-Type": "application/json"}),
                     json_body={"updates": {"security": {"allow_embedding": "true"}}},
                     timeout=30.0)
        print(f"  PUT  /api/admin/settings   {resp.status_code}  {resp.text[:80]}")
        if resp.status_code == 200:
            print("\n  NOTE: this stack now accepts the setting. Re-check whether the "
                  "console can iframe Grafana directly, and if so, delete this probe.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
