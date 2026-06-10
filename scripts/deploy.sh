#!/usr/bin/env bash
# Deploy Lossless to Google Cloud Run.
#
# Prereqs:
#   gcloud auth login
#   gcloud config set project YOUR_PROJECT
#   gcloud services enable run.googleapis.com aiplatform.googleapis.com
#
# Required env vars (read from your shell, NOT .env):
#   GOOGLE_CLOUD_PROJECT
#   GEMINI_MODEL                (default gemini-3-pro)
#   DT_ENVIRONMENT              (optional)
#   DT_PLATFORM_TOKEN           (optional)
#   GOOGLE_API_KEY              (optional — Vertex AI is used if unset and project is set)

set -euo pipefail

PROJECT="${GOOGLE_CLOUD_PROJECT:?Set GOOGLE_CLOUD_PROJECT first}"
REGION="${GOOGLE_CLOUD_LOCATION:-us-central1}"
SERVICE="${SERVICE:-lossless}"
MODEL="${GEMINI_MODEL:-gemini-3-pro}"
FALLBACK_MODEL="${GEMINI_FALLBACK_MODEL:-gemini-2.5-flash}"

ENV_VARS="GOOGLE_GENAI_USE_VERTEXAI=true"
ENV_VARS+=",GOOGLE_CLOUD_PROJECT=${PROJECT}"
ENV_VARS+=",GOOGLE_CLOUD_LOCATION=${REGION}"
ENV_VARS+=",GEMINI_MODEL=${MODEL}"
ENV_VARS+=",GEMINI_FALLBACK_MODEL=${FALLBACK_MODEL}"
ENV_VARS+=",DEMO_MODE=true"

if [[ -n "${DT_ENVIRONMENT:-}" && -n "${DT_PLATFORM_TOKEN:-}" ]]; then
  ENV_VARS+=",DT_ENVIRONMENT=${DT_ENVIRONMENT}"
  ENV_VARS+=",DT_PLATFORM_TOKEN=${DT_PLATFORM_TOKEN}"
  echo "→ Dynatrace MCP will be enabled"
else
  echo "→ Synthetic mode (Dynatrace not configured)"
fi

if [[ -n "${GOOGLE_API_KEY:-}" ]]; then
  ENV_VARS+=",GOOGLE_API_KEY=${GOOGLE_API_KEY}"
fi

echo "→ Deploying ${SERVICE} to ${REGION} in ${PROJECT}…"
gcloud run deploy "$SERVICE" \
  --source . \
  --region "$REGION" \
  --project "$PROJECT" \
  --allow-unauthenticated \
  --memory 2Gi \
  --cpu 2 \
  --port 8080 \
  --timeout 300 \
  --min-instances 1 \
  --max-instances 3 \
  --set-env-vars "^@^${ENV_VARS//,/@}"

URL=$(gcloud run services describe "$SERVICE" --region "$REGION" --format='value(status.url)')
echo "✓ Live at: $URL"
