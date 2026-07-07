#!/usr/bin/env bash
# Deploy Fluent Agents Studio in DEMO MODE to an Azure Web App.
#
# Usage:            ./scripts/deploy_azure_demo.sh
# Override names:   APP=my-name SKU=F1 ./scripts/deploy_azure_demo.sh
#
# Idempotent — safe to re-run for every redeploy: creates the plan and web
# app only if missing, then re-applies settings and pushes a fresh zip.
# Demo mode needs no ANTHROPIC_API_KEY, no ffmpeg, no cairo: the app serves
# the bundled demo/data read-only.
set -euo pipefail
cd "$(dirname "$0")/.."

RG="${RG:-voice}"
APP="${APP:-fluentagents-demo}"
PLAN="${PLAN:-fluentagents-plan}"
SKU="${SKU:-B1}"
RUNTIME="${RUNTIME:-PYTHON:3.12}"
LOCATION="${LOCATION:-$(az group show -n "$RG" --query location -o tsv)}"

echo "==> Resource group: $RG ($LOCATION) | app: $APP | plan: $PLAN ($SKU)"

# --- app service plan (linux) -------------------------------------------
if ! az appservice plan show -g "$RG" -n "$PLAN" >/dev/null 2>&1; then
  echo "==> Creating app service plan"
  az appservice plan create -g "$RG" -n "$PLAN" --sku "$SKU" --is-linux \
    --location "$LOCATION" -o none
fi

# --- web app --------------------------------------------------------------
if ! az webapp show -g "$RG" -n "$APP" >/dev/null 2>&1; then
  echo "==> Creating web app"
  az webapp create -g "$RG" -p "$PLAN" -n "$APP" --runtime "$RUNTIME" -o none
fi

echo "==> Applying settings (demo mode, build-on-deploy, startup command)"
az webapp config appsettings set -g "$RG" -n "$APP" -o none --settings \
  FLUENT_DEMO=1 \
  SCM_DO_BUILD_DURING_DEPLOYMENT=1 \
  WEBSITES_CONTAINER_START_TIME_LIMIT=600
az webapp config set -g "$RG" -n "$APP" -o none \
  --startup-file "python -m uvicorn app.main:app --host 0.0.0.0 --port 8000"

# --- package & deploy ------------------------------------------------------
echo "==> Packaging (app/, demo/, requirements.txt)"
ZIP="$(mktemp -d)/deploy.zip"
zip -qr "$ZIP" app demo requirements.txt -x "*__pycache__*" -x "*.DS_Store"
echo "    $(du -h "$ZIP" | cut -f1) $ZIP"

echo "==> Deploying (Oryx build runs remotely — takes a few minutes)"
az webapp deploy -g "$RG" -n "$APP" --src-path "$ZIP" --type zip \
  --timeout 900 -o none
rm -f "$ZIP"

URL="https://$(az webapp show -g "$RG" -n "$APP" --query defaultHostName -o tsv)"
echo "==> Deployed: $URL"
echo "==> Verifying demo mode…"
for i in $(seq 1 30); do
  body=$(curl -s --max-time 10 "$URL/config" || true)
  if [ "$body" = '{"demo":true}' ]; then
    echo "==> OK: $URL/config -> $body"
    exit 0
  fi
  sleep 10
done
echo "!! App did not report demo mode in time — check logs:"
echo "   az webapp log tail -g $RG -n $APP"
exit 1
