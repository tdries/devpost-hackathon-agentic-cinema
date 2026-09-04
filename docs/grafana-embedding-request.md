# Grafana Cloud support request: enable dashboard embedding

**Where to send it:** https://grafana.com/orgs/<your-org>/support (logged in as the
account that owns the stack), or the "Support" item in the Grafana Cloud portal.
Free-tier accounts may only get the community forum, and if there is no ticket
option, post the same text at https://community.grafana.com under "Grafana Cloud".

**What we are asking for:** Grafana's own embedding guide says authenticated
embedding on Cloud is "not enabled by default" and that Grafana enforces "a
tenant-scoped Content Security Policy (frame-ancestors) to list exact origins
allowed to embed dashboards". We want our one origin on that list.

**Stack:** dreamystairs2355 (stack id 1805312)
**Origin to allow:** `https://customs-app-akap4ao72a-ew.a.run.app`

---

## Paste this

Subject: Enable dashboard embedding (frame-ancestors allowlist) for stack dreamystairs2355

Hello,

I would like dashboard embedding enabled for my stack so that our application can
display live, interactive dashboards in an iframe.

- Stack slug: dreamystairs2355
- Stack id: 1805312
- Origin to add to the tenant-scoped frame-ancestors allowlist:
  https://customs-app-akap4ao72a-ew.a.run.app

Context: the application is an agentic ad-clearance console built for the Agentic
Cinema hackathon (Grafana Labs track). Its agents write to this stack over the
official grafana/mcp-grafana server: metrics to Mimir over OTLP, finding detail
to Loki, and dashboards, annotations and alert rules created by the agents
themselves, and an alert rule on one of those dashboards webhooks back into the
app to trigger automated remediation. The console currently renders your panels
server-side as PNGs via the render endpoint, because
`Content-Security-Policy: frame-ancestors 'none'` is returned on every dashboard
URL we have tried (`/d/`, `/d-solo/`, kiosk mode, and externally shared
dashboards), and `PUT /api/admin/settings` answers 403, as expected on Cloud.

What I would like to know:

1. Can the frame-ancestors allowlist be set for this stack with the origin above,
   and is that available on my current plan?
2. If it requires a paid plan or a contractual agreement, please tell me which,
   and I will evaluate it.
3. Is there any supported alternative for interactive embedding on Cloud today
   (externally shared dashboards with an embed URL form, snapshots, or an
   embedding SDK) that does not require the allowlist?

Thank you,
Tim Dries

---

## What we do while waiting

Nothing in the submission depends on the answer. The console keeps rendering
Grafana's own panels server-side, and the interactive path we control ships
regardless: a stock `grafana/grafana-oss` viewer with `allow_embedding = true`,
reading the same Loki and Mimir stores the crew writes to, iframed in the console.
The crew still writes to Grafana Cloud over MCP; the viewer is a second pair of
eyes on the same data.
