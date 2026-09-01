# The viewer: a stock Grafana that is allowed to be framed.
#
# Grafana Cloud refuses embedding -- every dashboard URL answers
# `frame-ancestors 'none'` and PUT /api/admin/settings answers 403, because
# on Cloud that switch belongs to Grafana (their own embedding guide:
# authenticated embedding is "not enabled by default", needs the account
# team, and is granted as a tenant-scoped frame-ancestors allowlist). So the
# console renders Cloud's panels as server-side PNGs.
#
# This image is the interactive half, and it is NOT a second copy of the
# data: its datasources proxy straight through the Cloud stack, with the
# same service account token the crew's agents use, at the same UIDs the
# dashboards reference (grafanacloud-logs, grafanacloud-prom) -- verified
# live before this was written. The crew still writes to Grafana Cloud over
# MCP; this is a second pair of eyes on the same stores, wearing
# allow_embedding=true so the console can put it in an iframe.
#
# ponytail: :latest, because a demo viewer that follows Grafana is worth
# more than a pin that rots. Pin the tag the day it surprises you.
FROM grafana/grafana-oss:latest

# Provisioning, not clicking: the datasources and the dashboards are files,
# so the viewer comes up already knowing everything the crew built.
COPY grafana-viewer/provisioning/ /etc/grafana/provisioning/
COPY grafana/dashboards/ /var/lib/grafana/dashboards/
