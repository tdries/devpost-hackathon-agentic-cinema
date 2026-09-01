#!/usr/bin/env bash
# Deploy the embeddable Grafana viewer.
#
# Why this exists: Grafana Cloud will not be framed. Every dashboard URL on
# the stack answers `Content-Security-Policy: frame-ancestors 'none'` on a
# real browser GET, and PUT /api/admin/settings answers 403 -- on Cloud that
# switch belongs to Grafana, granted per-tenant by their account team as an
# origin allowlist (docs/grafana-embedding-request.md is the ticket asking
# for exactly that). Until or unless that lands, this is the interactive
# Grafana the console can actually embed.
#
# It is NOT a second copy of the data. Its datasources proxy through the
# Cloud stack with the same service account token the crew's agents use, at
# the same UIDs the dashboards name, so what you see in the iframe is the
# same Loki and Mimir the Publisher wrote to thirty seconds ago.
#
# Usage:
#   scripts/deploy_viewer.sh            # build + deploy, print the URL
#   SERVICE=customs-grafana2 ...        # a fresh service name
#
# Afterwards, put the printed URL in .env as GRAFANA_VIEWER_URL and deploy
# the app (scripts/deploy.sh) so the console starts embedding it.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PROJECT_ID="veoapi-469317"
REGION="${REGION:-europe-west1}"
SERVICE="${SERVICE:-customs-grafana}"
# The app's own runtime identity: it already holds secretAccessor, and the
# default compute SA does not.
RUNTIME_SA="veo-api-user@${PROJECT_ID}.iam.gserviceaccount.com"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/cloud-run-source-deploy/grafana-viewer"
ENV_FILE="${ENV_FILE:-.env}"

if [[ ! -f "$ENV_FILE" ]]; then
    echo "deploy_viewer.sh: $ENV_FILE not found." >&2
    exit 1
fi
# shellcheck disable=SC1090
set -a; . "./$ENV_FILE"; set +a
STACK_URL="${GRAFANA_URL%/}"
if [[ -z "${STACK_URL:-}" ]]; then
    echo "deploy_viewer.sh: GRAFANA_URL is not set in $ENV_FILE." >&2
    exit 1
fi

# Where Grafana Cloud actually keeps this stack's Loki and Mimir, and which
# tenant id each wants as its basic-auth user -- asked of the stack rather
# than pasted in, so a moved tenant fixes itself on the next deploy.
read -r LOKI_URL LOKI_USER < <(curl -s -H "Authorization: Bearer $GRAFANA_SA_TOKEN" \
    "$STACK_URL/api/datasources/uid/grafanacloud-logs" \
    | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["url"], d["basicAuthUser"])')
read -r PROM_URL PROM_USER < <(curl -s -H "Authorization: Bearer $GRAFANA_SA_TOKEN" \
    "$STACK_URL/api/datasources/uid/grafanacloud-prom" \
    | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["url"], d["basicAuthUser"])')
if [[ -z "${LOKI_URL:-}" || -z "${PROM_URL:-}" ]]; then
    echo "deploy_viewer.sh: could not read the stack's datasource endpoints." >&2
    exit 1
fi
echo "-- loki $LOKI_URL (tenant $LOKI_USER) | mimir $PROM_URL (tenant $PROM_USER) --"

echo "-- building $IMAGE --"
gcloud builds submit --project "$PROJECT_ID" --region "$REGION" \
    --config grafana-viewer/cloudbuild.yaml \
    --substitutions "_IMAGE=${IMAGE}" --quiet

# The env that makes a stock Grafana embeddable and anonymous-readable:
#
#   ALLOW_EMBEDDING       the whole point -- no frame-ancestors refusal
#   COOKIE_SAMESITE=none  a cross-origin iframe drops SameSite=Lax cookies,
#                         and without a session Grafana redirects to login
#   ANONYMOUS Viewer      nobody should have to log in to read a panel that
#                         is already public on the console
#   DISABLE_LOGIN_FORM    there is no account to log into; do not offer one
#
# The containment is the TOKEN, not the UI. An anonymous Viewer can reach
# POST /api/ds/query whatever the Explore setting says, so the credential
# this service carries is a purpose-minted access policy: logs:read +
# metrics:read, pinned by labelPolicy to {app="customs"}, and refused by
# grafana.com's account API. Never mount the stack's admin SA token here.
ENV_VARS="GF_SERVER_HTTP_PORT=8080"
ENV_VARS+=",GF_SECURITY_ALLOW_EMBEDDING=true"
ENV_VARS+=",GF_SECURITY_COOKIE_SAMESITE=none"
ENV_VARS+=",GF_SECURITY_COOKIE_SECURE=true"
ENV_VARS+=",GF_AUTH_ANONYMOUS_ENABLED=true"
ENV_VARS+=",GF_AUTH_ANONYMOUS_ORG_ROLE=Viewer"
ENV_VARS+=",GF_AUTH_DISABLE_LOGIN_FORM=true"
ENV_VARS+=",GF_AUTH_BASIC_ENABLED=false"
ENV_VARS+=",GF_USERS_ALLOW_SIGN_UP=false"
ENV_VARS+=",GF_USERS_VIEWERS_CAN_EDIT=false"
ENV_VARS+=",GF_ANALYTICS_REPORTING_ENABLED=false"
ENV_VARS+=",GF_ANALYTICS_CHECK_FOR_UPDATES=false"
ENV_VARS+=",GF_NEWS_NEWS_FEED_ENABLED=false"
# Explore off. It does not close POST /api/ds/query -- an anonymous caller
# can still run a query the API way -- which is why the token itself is
# scoped and label-pinned rather than trusted to the UI.
ENV_VARS+=",GF_EXPLORE_ENABLED=false"
ENV_VARS+=",GF_SNAPSHOTS_EXTERNAL_ENABLED=false"
ENV_VARS+=",LOKI_URL=${LOKI_URL},LOKI_USER=${LOKI_USER}"
ENV_VARS+=",PROM_URL=${PROM_URL},PROM_USER=${PROM_USER}"

echo "-- deploying $SERVICE --"
gcloud run deploy "$SERVICE" \
    --project "$PROJECT_ID" --region "$REGION" \
    --image "${IMAGE}:latest" \
    --allow-unauthenticated \
    --service-account "$RUNTIME_SA" \
    --port 8080 \
    --memory 1Gi \
    --min-instances 0 --max-instances 1 \
    --set-env-vars "$ENV_VARS" \
    --set-secrets "VIEWER_TOKEN=grafana-viewer-token:latest" \
    --quiet

URL="$(gcloud run services describe "$SERVICE" --project "$PROJECT_ID" \
        --region "$REGION" --format 'value(status.url)')"

# GF_SERVER_ROOT_URL has to be the service's own URL, which does not exist
# until the first deploy has created it. Second pass, once.
gcloud run services update "$SERVICE" \
    --project "$PROJECT_ID" --region "$REGION" \
    --update-env-vars "GF_SERVER_ROOT_URL=${URL}" --quiet >/dev/null

echo "== viewer deployed =="
echo "$URL"
echo
echo "Put this in .env and redeploy the app to embed it:"
echo "  GRAFANA_VIEWER_URL=${URL}"
