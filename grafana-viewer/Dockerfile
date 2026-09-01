# The viewer: a stock Grafana that is allowed to be framed.
#
# Grafana Cloud refuses embedding -- every dashboard URL answers
# `frame-ancestors 'none'` on the GET a browser actually issues, and PUT
# /api/admin/settings answers 403 (that endpoint only ever supported
# auth.saml, and is deprecated). On Cloud the switch belongs to Grafana:
# their embedding guide grants it per tenant, through the account team, as a
# frame-ancestors allowlist. docs/grafana-embedding-request.md is that ticket.
#
# So this is the interactive half, and it holds no data of its own: its
# datasources read Grafana Cloud directly with a purpose-minted read-only,
# label-pinned token, at the same UIDs the crew's dashboards name. The crew
# still writes to Grafana Cloud over MCP; this is a second pair of eyes on
# the same stores.
#
# Pinned, not :latest -- and 13.0.2 is the newest grafana-oss tag that
# exists (the Cloud stack runs 13.3.0 Enterprise, which has no OSS build).
# The dashboards are schemaVersion 39 and every panel type in them is core,
# so 13.0.2 migrates them forward cleanly.
FROM grafana/grafana-oss:13.0.2

# An allowlist rather than a free-for-all. GF_SECURITY_ALLOW_EMBEDDING alone
# strips the framing headers entirely; enabling the CSP with this template
# instead emits `frame-ancestors 'self' <the console>`, so only the console
# may frame it. Baked in here rather than passed through the deploy script,
# because the template is full of $-placeholders Grafana expands itself and
# a shell has no business touching them.
ENV GF_SECURITY_CONTENT_SECURITY_POLICY=true
ENV GF_SECURITY_CONTENT_SECURITY_POLICY_TEMPLATE="script-src 'self' 'unsafe-eval' 'unsafe-inline' 'strict-dynamic' \$NONCE;object-src 'none';font-src 'self';style-src 'self' 'unsafe-inline' blob:;img-src * data:;base-uri 'self';connect-src 'self' grafana.com ws://\$ROOT_PATH wss://\$ROOT_PATH;manifest-src 'self';media-src 'none';form-action 'self';frame-ancestors 'self' https://customs-app-akap4ao72a-ew.a.run.app;"

# Provisioning, not clicking: datasources and dashboards are files, so the
# viewer boots already knowing everything the crew built. The empty plugins
# and alerting directories exist only to stop Grafana logging a provisioning
# error on every boot.
COPY grafana-viewer/provisioning/ /etc/grafana/provisioning/
COPY grafana/dashboards/ /var/lib/grafana/dashboards/
