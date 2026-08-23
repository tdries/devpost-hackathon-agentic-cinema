#!/usr/bin/env bash
# Deploy Customs to Cloud Run. Idempotent: every step below is safe to run
# again -- API enablement, the two Grafana secrets, the IAM grants and the
# service deploy itself all update in place rather than piling up copies or
# erroring on "already exists".
#
# Usage:
#   scripts/deploy.sh                          # europe-west1, service "customs-app"
#   REGION=europe-west4 scripts/deploy.sh      # capacity/quota fallback region
#   SERVICE=customs-app2 scripts/deploy.sh     # fresh service name (see below)
#
# SERVICE defaults to "customs-app", not "customs": the first "customs"
# deploys in this project (both regions) came up Ready=True with a healthy
# container and correct IAM/ingress, but their public URLs never became
# reachable at Google's edge (zero request logs, generic edge 404) while a
# throwaway hello-world service in the same project and region served
# immediately -- so whatever wedged, it was scoped to those two service
# names, not the project or account. A fresh SERVICE name is the workaround;
# the two old "customs" services are cleanup candidates once a new name is
# confirmed reachable, not a config bug here.
#
# What this does, in order:
#   1. Enables the GCP APIs the rest of this script and the running service
#      need (Cloud Run, Cloud Build, Artifact Registry, Secret Manager,
#      Vertex AI).
#   2. Creates or updates the two Grafana secrets in Secret Manager from
#      .env, piped in on stdin so the value is never a command argument,
#      never in shell history, never in this script's own output.
#   3. Grants the runtime service account roles/aiplatform.user (Vertex
#      calls) and roles/secretmanager.secretAccessor on those two secrets
#      (so --set-secrets below can actually mount them).
#   4. `gcloud run deploy`: single instance (SQLite + in-process locks do
#      not survive a second replica -- ponytail tradeoff, not an oversight),
#      every non-secret config value from .env as a plain env var, the two
#      tokens as --set-secrets.
#   5. Points the customs-webhook Grafana contact point at this service's
#      real /webhook/alert URL, replacing whatever it pointed at before
#      (scripts/provision_grafana.py's placeholder, or an earlier deploy's
#      URL). GrafanaOps.ensure_contact_point is idempotent by contact point
#      name, so this is safe to run on every deploy.
#   6. Prints the service URL, and only the service URL, on its own final
#      line.
#
# Runtime identity: veo-api-user@veoapi-469317.iam.gserviceaccount.com
# rather than the default compute service account. It already exists with
# exactly roles/aiplatform.user (see the project's IAM policy) and nothing
# broader, which is a meaningfully tighter grant than the default compute
# SA's project-wide roles/editor for a service that is public
# (--allow-unauthenticated) and accepts file uploads from the internet.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PROJECT_ID="veoapi-469317"
REGION="${REGION:-europe-west1}"
SERVICE="${SERVICE:-customs-app}"
RUNTIME_SA="veo-api-user@${PROJECT_ID}.iam.gserviceaccount.com"
ENV_FILE="${ENV_FILE:-.env}"
PY="$ROOT/.venv/bin/python"

if [[ ! -x "$PY" ]]; then
    echo "deploy.sh: $PY not found. Create the venv first (python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt)." >&2
    exit 1
fi
if [[ ! -f "$ENV_FILE" ]]; then
    echo "deploy.sh: $ENV_FILE not found. See .env.example for the required contract." >&2
    exit 1
fi

echo "== customs -> Cloud Run ($PROJECT_ID / $REGION, service account $RUNTIME_SA) =="

echo "-- enabling required APIs (idempotent) --"
gcloud services enable \
    run.googleapis.com \
    cloudbuild.googleapis.com \
    artifactregistry.googleapis.com \
    secretmanager.googleapis.com \
    aiplatform.googleapis.com \
    --project "$PROJECT_ID" --quiet

# -- pull every value this script needs out of .env, through the app's own
# Settings loader rather than reimplementing its parsing here. Read over a
# process substitution (not a pipe) so `read` sets variables in *this*
# shell, and never through anything that echoes: the two token lines never
# touch a terminal, a log, or this script's own stdout/stderr.
{
    IFS= read -r DEPLOY_GRAFANA_URL
    IFS= read -r DEPLOY_GRAFANA_STACK_ID
    IFS= read -r DEPLOY_GRAFANA_SA_TOKEN
    IFS= read -r DEPLOY_GRAFANA_CLOUD_TOKEN
    IFS= read -r DEPLOY_OTLP_URL
    IFS= read -r DEPLOY_LOKI_PUSH_URL
    IFS= read -r DEPLOY_LOKI_USER
    IFS= read -r DEPLOY_GEMINI_MODEL_VISION
    IFS= read -r DEPLOY_GEMINI_MODEL_TEXT
    IFS= read -r DEPLOY_IMAGEN_MODEL
    IFS= read -r DEPLOY_VEO_MODEL
    IFS= read -r DEPLOY_TTS_MODEL
} < <(PYTHONPATH="$ROOT/src" "$PY" - "$ENV_FILE" <<'PYEOF'
import sys
from customs.config import Settings

s = Settings.load(sys.argv[1])
for value in (s.grafana_url, s.grafana_stack_id, s.grafana_sa_token,
              s.grafana_cloud_token, s.otlp_url, s.loki_push_url, s.loki_user,
              s.model_vision, s.model_text, s.model_image, s.model_video,
              s.model_tts):
    print(value)
PYEOF
)

for pair in "GRAFANA_URL:$DEPLOY_GRAFANA_URL" "GRAFANA_STACK_ID:$DEPLOY_GRAFANA_STACK_ID" \
            "GRAFANA_SA_TOKEN:$DEPLOY_GRAFANA_SA_TOKEN" "GRAFANA_CLOUD_TOKEN:$DEPLOY_GRAFANA_CLOUD_TOKEN" \
            "OTLP_URL:$DEPLOY_OTLP_URL" "LOKI_PUSH_URL:$DEPLOY_LOKI_PUSH_URL" \
            "LOKI_USER:$DEPLOY_LOKI_USER"; do
    name="${pair%%:*}"
    val="${pair#*:}"
    if [[ -z "$val" ]]; then
        echo "deploy.sh: $name is required in $ENV_FILE and is empty or missing." >&2
        exit 1
    fi
done

# -- Secret Manager: create on first run, add a version on every re-run.
# --data-file=- reads the payload from stdin so it is never a command
# argument (never in `ps`, never in shell history) and this function never
# prints $value.
put_secret() {
    local name="$1" value="$2"
    if gcloud secrets describe "$name" --project "$PROJECT_ID" >/dev/null 2>&1; then
        printf '%s' "$value" | gcloud secrets versions add "$name" \
            --project "$PROJECT_ID" --data-file=- >/dev/null
        echo "secret $name: new version added"
    else
        printf '%s' "$value" | gcloud secrets create "$name" \
            --project "$PROJECT_ID" --data-file=- --replication-policy=automatic >/dev/null
        echo "secret $name: created"
    fi
}

echo "-- Secret Manager --"
put_secret grafana-sa-token "$DEPLOY_GRAFANA_SA_TOKEN"
put_secret grafana-cloud-token "$DEPLOY_GRAFANA_CLOUD_TOKEN"

echo "-- IAM: $RUNTIME_SA --"
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member "serviceAccount:${RUNTIME_SA}" \
    --role roles/aiplatform.user \
    --condition=None --quiet >/dev/null
echo "  roles/aiplatform.user"

for secret in grafana-sa-token grafana-cloud-token; do
    gcloud secrets add-iam-policy-binding "$secret" \
        --project "$PROJECT_ID" \
        --member "serviceAccount:${RUNTIME_SA}" \
        --role roles/secretmanager.secretAccessor --quiet >/dev/null
    echo "  roles/secretmanager.secretAccessor on $secret"
done

# -- the deploy. GOOGLE_CLOUD_LOCATION tracks the region actually deployed
# to (not whatever .env says), so a europe-west4 capacity fallback reports
# its own real location rather than a stale europe-west1 pin. --
env_pairs=(
    "GOOGLE_CLOUD_PROJECT=${PROJECT_ID}"
    "GOOGLE_CLOUD_LOCATION=${REGION}"
    "GOOGLE_GENAI_USE_VERTEXAI=true"
    "GRAFANA_URL=${DEPLOY_GRAFANA_URL}"
    "GRAFANA_STACK_ID=${DEPLOY_GRAFANA_STACK_ID}"
    "OTLP_URL=${DEPLOY_OTLP_URL}"
    "LOKI_PUSH_URL=${DEPLOY_LOKI_PUSH_URL}"
    "LOKI_USER=${DEPLOY_LOKI_USER}"
)
if [[ -n "$DEPLOY_GEMINI_MODEL_VISION" ]]; then env_pairs+=("GEMINI_MODEL_VISION=${DEPLOY_GEMINI_MODEL_VISION}"); fi
if [[ -n "$DEPLOY_GEMINI_MODEL_TEXT" ]]; then env_pairs+=("GEMINI_MODEL_TEXT=${DEPLOY_GEMINI_MODEL_TEXT}"); fi
if [[ -n "$DEPLOY_IMAGEN_MODEL" ]]; then env_pairs+=("IMAGEN_MODEL=${DEPLOY_IMAGEN_MODEL}"); fi
if [[ -n "$DEPLOY_VEO_MODEL" ]]; then env_pairs+=("VEO_MODEL=${DEPLOY_VEO_MODEL}"); fi
if [[ -n "$DEPLOY_TTS_MODEL" ]]; then env_pairs+=("TTS_MODEL=${DEPLOY_TTS_MODEL}"); fi

joined="$(IFS=';'; echo "${env_pairs[*]}")"

echo "-- gcloud run deploy $SERVICE --"
gcloud run deploy "$SERVICE" \
    --source . \
    --project "$PROJECT_ID" \
    --region "$REGION" \
    --service-account "$RUNTIME_SA" \
    --allow-unauthenticated \
    --max-instances 1 --min-instances 1 \
    --concurrency 20 \
    --memory 2Gi --cpu 2 \
    --timeout 900 \
    --set-env-vars "^;^${joined}" \
    --set-secrets "GRAFANA_SA_TOKEN=grafana-sa-token:latest,GRAFANA_CLOUD_TOKEN=grafana-cloud-token:latest" \
    --quiet

SERVICE_URL="$(gcloud run services describe "$SERVICE" \
    --project "$PROJECT_ID" --region "$REGION" --format='value(status.url)')"

echo "-- wiring the Grafana contact point to ${SERVICE_URL}/webhook/alert --"
PYTHONPATH="$ROOT/src" "$PY" - "$SERVICE_URL" <<'PYEOF'
import sys
from customs.config import Settings
from customs.grafana_ops import GrafanaOps

url = sys.argv[1].rstrip("/") + "/webhook/alert"
settings = Settings.load()
with GrafanaOps(settings, mcp_tools=set()) as ops:
    uid = ops.ensure_contact_point(url)
print(f"customs-webhook -> {url} (uid {uid or 'existing'})")
PYEOF

echo "== deployed =="
echo "$SERVICE_URL"
