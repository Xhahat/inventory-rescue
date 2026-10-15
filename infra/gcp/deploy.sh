#!/usr/bin/env bash
# One-shot Google Cloud deployment for Inventory Rescue (idempotent; safe to re-run).
# Run from the repo root in Cloud Shell or any shell with gcloud + openssl:
#
#   PROJECT_ID=my-project ./infra/gcp/deploy.sh            # private service (IAM-authenticated)
#   PROJECT_ID=my-project PUBLIC=1 ./infra/gcp/deploy.sh   # public demo URL
#
# LLM: with GEMINI_API_KEY set (a Google AI Studio key) the key is stored in Secret Manager and the service calls the
# Gemini API; without it the service uses Gemini on Vertex AI through its service account.
#
#   GEMINI_API_KEY=... PROJECT_ID=my-project PUBLIC=1 ./infra/gcp/deploy.sh
#
# Creates: required APIs, Artifact Registry repo, runtime service account, Cloud Storage bucket,
# Cloud SQL for PostgreSQL 16 instance + database + user, Secret Manager secret holding DATABASE_URL,
# a Secret Manager secret holding the Gemini API key (if given), the Cloud Run service, and a Cloud Run job that seeds the demo data (run once).
set -euo pipefail

PROJECT_ID="${PROJECT_ID:?set PROJECT_ID}"
REGION="${REGION:-asia-south1}"
SERVICE="${SERVICE:-inventory-rescue}"
REPO="${REPO:-inventory-rescue}"
SQL_INSTANCE="${SQL_INSTANCE:-inventory-rescue-pg}"
SQL_TIER="${SQL_TIER:-db-f1-micro}"          # smallest shared-core tier; use db-g1-small or larger for real load
MIN_INSTANCES="${MIN_INSTANCES:-0}"         # 0 = scale to zero when idle (first request after idle takes a few seconds)
MAX_INSTANCES="${MAX_INSTANCES:-2}"
DB_NAME="${DB_NAME:-inventory_rescue}"
DB_USER="${DB_USER:-app}"
BUCKET="${BUCKET:-${PROJECT_ID}-inventory-rescue}"
SA_NAME="${SA_NAME:-inventory-rescue-run}"
DB_SECRET="${DB_SECRET:-inventory-rescue-database-url}"
GEMINI_MODEL="${GEMINI_MODEL:-gemini-3.5-flash-lite}"
GEMINI_API_KEY="${GEMINI_API_KEY:-}"
KEY_SECRET="${KEY_SECRET:-inventory-rescue-gemini-api-key}"
GCP_LOCATION="${GCP_LOCATION:-global}"
SEED="${SEED:-1}"
PUBLIC="${PUBLIC:-0}"

SA="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/app"
CONN="${PROJECT_ID}:${REGION}:${SQL_INSTANCE}"
TAG="$(date +%Y%m%d-%H%M%S)"

say() { printf '\n==> %s\n' "$*"; }
gcloud config set project "$PROJECT_ID" >/dev/null

say "Enabling APIs"
gcloud services enable run.googleapis.com sqladmin.googleapis.com aiplatform.googleapis.com \
  artifactregistry.googleapis.com cloudbuild.googleapis.com secretmanager.googleapis.com \
  storage.googleapis.com iam.googleapis.com

say "Artifact Registry: ${REPO}"
gcloud artifacts repositories describe "$REPO" --location "$REGION" >/dev/null 2>&1 ||
  gcloud artifacts repositories create "$REPO" --repository-format docker --location "$REGION"

say "Runtime service account: ${SA}"
gcloud iam service-accounts describe "$SA" >/dev/null 2>&1 ||
  gcloud iam service-accounts create "$SA_NAME" --display-name "Inventory Rescue (Cloud Run)"
for role in roles/aiplatform.user roles/cloudsql.client roles/logging.logWriter; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" --member "serviceAccount:${SA}" --role "$role" \
    --condition None >/dev/null
done

say "Cloud Storage bucket: gs://${BUCKET}"
gcloud storage buckets describe "gs://${BUCKET}" >/dev/null 2>&1 ||
  gcloud storage buckets create "gs://${BUCKET}" --location "$REGION" --uniform-bucket-level-access \
    --public-access-prevention
gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" --member "serviceAccount:${SA}" \
  --role roles/storage.objectAdmin >/dev/null

say "Cloud SQL for PostgreSQL 16: ${SQL_INSTANCE} (first creation takes several minutes)"
gcloud sql instances describe "$SQL_INSTANCE" >/dev/null 2>&1 ||
  gcloud sql instances create "$SQL_INSTANCE" --database-version POSTGRES_16 --edition enterprise \
    --tier "$SQL_TIER" --region "$REGION" --storage-size 10 --storage-auto-increase
gcloud sql databases describe "$DB_NAME" --instance "$SQL_INSTANCE" >/dev/null 2>&1 ||
  gcloud sql databases create "$DB_NAME" --instance "$SQL_INSTANCE"

say "Database credentials in Secret Manager: ${DB_SECRET}"
if ! gcloud secrets describe "$DB_SECRET" >/dev/null 2>&1; then
  DB_PASS="$(openssl rand -hex 24)"
  if gcloud sql users list --instance "$SQL_INSTANCE" --format 'value(name)' | grep -qx "$DB_USER"; then
    gcloud sql users set-password "$DB_USER" --instance "$SQL_INSTANCE" --password "$DB_PASS"
  else
    gcloud sql users create "$DB_USER" --instance "$SQL_INSTANCE" --password "$DB_PASS"
  fi
  printf 'postgresql+psycopg://%s:%s@/%s?host=/cloudsql/%s' "$DB_USER" "$DB_PASS" "$DB_NAME" "$CONN" |
    gcloud secrets create "$DB_SECRET" --replication-policy automatic --data-file -
  unset DB_PASS
fi
gcloud secrets add-iam-policy-binding "$DB_SECRET" --member "serviceAccount:${SA}" \
  --role roles/secretmanager.secretAccessor >/dev/null

SECRETS="DATABASE_URL=${DB_SECRET}:latest"
if [[ -n "$GEMINI_API_KEY" ]]; then
  say "Gemini API key in Secret Manager: ${KEY_SECRET}"
  if gcloud secrets describe "$KEY_SECRET" >/dev/null 2>&1; then
    printf '%s' "$GEMINI_API_KEY" | gcloud secrets versions add "$KEY_SECRET" --data-file -
  else
    printf '%s' "$GEMINI_API_KEY" | gcloud secrets create "$KEY_SECRET" --replication-policy automatic --data-file -
  fi
  gcloud secrets add-iam-policy-binding "$KEY_SECRET" --member "serviceAccount:${SA}" \
    --role roles/secretmanager.secretAccessor >/dev/null
  SECRETS="${SECRETS},GEMINI_API_KEY=${KEY_SECRET}:latest"
  LLM_ENV="LLM_PROVIDER=gemini"
else
  LLM_ENV="LLM_PROVIDER=vertex,GOOGLE_CLOUD_PROJECT=${PROJECT_ID},GCP_LOCATION=${GCP_LOCATION}"
fi
unset GEMINI_API_KEY

say "Building image ${IMAGE}:${TAG} with Cloud Build"
gcloud builds submit --tag "${IMAGE}:${TAG}" .

ENV_VARS="${LLM_ENV},GEMINI_MODEL=${GEMINI_MODEL}"
ENV_VARS="${ENV_VARS},STORAGE_BACKEND=gcs,GCS_BUCKET=${BUCKET}"

if [[ "$PUBLIC" == "1" ]]; then AUTH_FLAG="--allow-unauthenticated"; else AUTH_FLAG="--no-allow-unauthenticated"; fi

say "Deploying Cloud Run service: ${SERVICE}"
# --no-cpu-throttling: plan robustness evaluation and forecast refresh run as background work after the HTTP
# response returns, so the CPU must stay allocated while an instance is up. MIN_INSTANCES=1 keeps one warm.
gcloud run deploy "$SERVICE" --image "${IMAGE}:${TAG}" --region "$REGION" --service-account "$SA" \
  --set-cloudsql-instances "$CONN" --set-secrets "$SECRETS" \
  --set-env-vars "$ENV_VARS" --cpu 2 --memory 2Gi --no-cpu-throttling \
  --min-instances "$MIN_INSTANCES" --max-instances "$MAX_INSTANCES" \
  --concurrency 40 --timeout 300 "$AUTH_FLAG" --quiet

say "Seed job: ${SERVICE}-seed"
gcloud run jobs deploy "${SERVICE}-seed" --image "${IMAGE}:${TAG}" --region "$REGION" --service-account "$SA" \
  --set-cloudsql-instances "$CONN" --set-secrets "$SECRETS" \
  --set-env-vars "$ENV_VARS" --cpu 2 --memory 2Gi --task-timeout 1800 --max-retries 0 \
  --command python --args=-m,scripts.seed,--no-generate --quiet
if [[ "$SEED" == "1" ]]; then
  gcloud run jobs execute "${SERVICE}-seed" --region "$REGION" --wait
fi

URL="$(gcloud run services describe "$SERVICE" --region "$REGION" --format 'value(status.url)')"
say "Done: ${URL}"
if [[ "$PUBLIC" != "1" ]]; then
  echo "The service requires IAM auth. Open it locally with:"
  echo "  gcloud run services proxy ${SERVICE} --region ${REGION} --port 8080   # then http://localhost:8080"
fi
