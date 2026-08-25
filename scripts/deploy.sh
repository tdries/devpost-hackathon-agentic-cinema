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

# Preflight. `set -e` already aborts on a missing binary, but the message
# is "gcloud: command not found" from whichever line hit it first, which
# reads like a step failing rather than the tool being absent. Worse, a
# caller who pipes this script to `tail` sees the PIPELINE's status --
# tail's, which is 0 -- and believes a deploy happened that did not.
for tool in gcloud docker git; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "FATAL: $tool is not on PATH. Nothing was deployed." >&2
        echo "  PATH=$PATH" >&2
        [ "$tool" = "gcloud" ] && echo "  try: export PATH=/opt/homebrew/bin:\$PATH" >&2
        exit 78
    fi
done

echo "== customs -> Cloud Run ($PROJECT_ID / $REGION, service account $RUNTIME_SA) =="

# FAST=1 skips everything below that only has to be true ONCE: the APIs,
# the secrets, the IAM grants, the bucket and the Grafana contact point.
# They are idempotent, which is exactly why re-running them on the tenth
# deploy of an hour changes nothing and still costs the wall clock. Use it
# for a code change; leave it off after touching .env, IAM or the stack.
FAST="${FAST:-0}"
if [ "$FAST" = "1" ]; then
    echo "-- FAST=1: skipping APIs, secrets, IAM and Grafana wiring --"
fi

if [ "$FAST" != "1" ]; then
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

# Optional: YouTube cookies for the link intake. YouTube bot-challenges
# Google Cloud IPs even on the tv/android player clients, so if the operator
# has created a yt-cookies secret (a Netscape-format export, see .env.example)
# it is mounted read-only and fetch.py picks it up via YT_COOKIES_FILE.
# Create it with:  gcloud secrets create yt-cookies --data-file=cookies.txt
SET_SECRETS="GRAFANA_SA_TOKEN=grafana-sa-token:latest,GRAFANA_CLOUD_TOKEN=grafana-cloud-token:latest"
GRANT_SECRETS=(grafana-sa-token grafana-cloud-token)
YT_COOKIES_ENV=()
if gcloud secrets describe yt-cookies --project "$PROJECT_ID" >/dev/null 2>&1; then
    SET_SECRETS+=",/secrets/yt/cookies.txt=yt-cookies:latest"
    GRANT_SECRETS+=(yt-cookies)
    YT_COOKIES_ENV=("YT_COOKIES_FILE=/secrets/yt/cookies.txt")
    echo "secret yt-cookies: found, will be mounted"
else
    echo "secret yt-cookies: not present, YouTube intake runs without cookies"
fi

# -- the state bucket. Cloud Run hands every revision a fresh filesystem, so
# the run store and its artifacts are mirrored to this bucket and restored on
# boot (see customs/persist.py). Created on first deploy, then reused. --
STATE_BUCKET="${STATE_BUCKET:-${PROJECT_ID}-customs-state}"
if ! gcloud storage buckets describe "gs://${STATE_BUCKET}" --project "$PROJECT_ID" >/dev/null 2>&1; then
    gcloud storage buckets create "gs://${STATE_BUCKET}" \
        --project "$PROJECT_ID" --location "$REGION" --uniform-bucket-level-access --quiet
    echo "state bucket gs://${STATE_BUCKET}: created"
else
    echo "state bucket gs://${STATE_BUCKET}: present"
fi
gcloud storage buckets add-iam-policy-binding "gs://${STATE_BUCKET}" \
    --project "$PROJECT_ID" --member "serviceAccount:${RUNTIME_SA}" \
    --role roles/storage.objectAdmin --quiet >/dev/null
echo "  roles/storage.objectAdmin on gs://${STATE_BUCKET}"

echo "-- IAM: $RUNTIME_SA --"
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member "serviceAccount:${RUNTIME_SA}" \
    --role roles/aiplatform.user \
    --condition=None --quiet >/dev/null
echo "  roles/aiplatform.user"

for secret in "${GRANT_SECRETS[@]}"; do
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
    "CUSTOMS_STATE_DIR=/mnt/state"
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
if [[ ${#YT_COOKIES_ENV[@]} -gt 0 ]]; then env_pairs+=("${YT_COOKIES_ENV[@]}"); fi

joined="$(IFS=';'; echo "${env_pairs[*]}")"

fi   # end of the once-only preamble

# Build with layer caching rather than gcloud run deploy --source, which
# starts every build from nothing. See cloudbuild.yaml: the previous image
# is pulled so Docker can reuse its layers, and the Dockerfile is already
# ordered so an ordinary source edit invalidates only COPY src/ and below.
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/cloud-run-source-deploy/${SERVICE}"
SHA="$(git rev-parse --short HEAD 2>/dev/null || date +%s)"

echo "-- building ${IMAGE}:${SHA} (cached) --"
gcloud builds submit \
    --project "$PROJECT_ID" --region "$REGION" \
    --config cloudbuild.yaml \
    --substitutions "_IMAGE=${IMAGE},SHORT_SHA=${SHA}" \
    --quiet

echo "-- gcloud run deploy $SERVICE --"
gcloud run deploy "$SERVICE" \
    --image "${IMAGE}:${SHA}" \
    --project "$PROJECT_ID" \
    --region "$REGION" \
    --service-account "$RUNTIME_SA" \
    --allow-unauthenticated \
    --max-instances 1 --min-instances 1 \
    --concurrency 20 \
    --memory 2Gi --cpu 2 \
    --timeout 900 \
    --set-env-vars "^;^${joined}" \
    --set-secrets "$SET_SECRETS" \
    --add-volume "name=state,type=cloud-storage,bucket=${STATE_BUCKET}" \
    --add-volume-mount "volume=state,mount-path=/mnt/state" \
    --quiet

SERVICE_URL="$(gcloud run services describe "$SERVICE" \
    --project "$PROJECT_ID" --region "$REGION" --format='value(status.url)')"

if [ "$FAST" != "1" ]; then
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

fi   # end of the Grafana wiring

echo "== deployed =="
echo "$SERVICE_URL"
