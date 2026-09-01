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
IMAGE="europe-west1-docker.pkg.dev/${PROJECT_ID}/customs/grafana-viewer"
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

echo "-- building $IMAGE --"
gcloud builds submit --project "$PROJECT_ID" --region "$REGION" \
    --tag "${IMAGE}:latest" --file grafana-viewer/Dockerfile . --quiet

# The env that makes a stock Grafana embeddable and anonymous-readable:
#
#   ALLOW_EMBEDDING       the whole point -- no frame-ancestors refusal
#   COOKIE_SAMESITE=none  a cross-origin iframe drops SameSite=Lax cookies,
#                         and without a session Grafana redirects to login
#   ANONYMOUS Viewer      nobody should have to log in to read a panel that
#                         is already public on the console
#   DISABLE_LOGIN_FORM    there is no account to log into; do not offer one
#
# ponytail: anonymous Viewer can also open Explore and run queries against
# this stack. The stack holds only this project's clearance telemetry, which
# the console publishes anyway, so the ceiling is stated rather than fenced.
# Fence it with GF_USERS_VIEWERS_CAN_EDIT=false plus an auth proxy the day
# it holds anything else.
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
ENV_VARS+=",STACK_URL=${STACK_URL}"

echo "-- deploying $SERVICE --"
gcloud run deploy "$SERVICE" \
    --project "$PROJECT_ID" --region "$REGION" \
    --image "${IMAGE}:latest" \
    --allow-unauthenticated \
    --port 8080 \
    --memory 512Mi \
    --min-instances 0 --max-instances 1 \
    --set-env-vars "$ENV_VARS" \
    --set-secrets "SA_TOKEN=grafana-sa-token:latest" \
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
