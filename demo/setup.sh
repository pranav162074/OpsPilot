#!/usr/bin/env bash
# Connects your OpsPilot workspace to the demo:
#   - Prometheus, Loki and Kuturl's GitHub repository as telemetry integrations
#   - a responder API key for Alertmanager, so alerts become incidents
#   - Kuturl's runbooks in the knowledge base
#   - the first Kuturl deploy, so "what changed?" has a baseline
#
# Usage (Git Bash on Windows, or any bash), with the OpsPilot API running on :8000:
#   OPSPILOT_API_KEY=ops_... bash demo/setup.sh        # an ADMIN key of your workspace
set -euo pipefail
cd "$(dirname "$0")/.."

API="${OPSPILOT_URL:-http://localhost:8000}"
KEY="${OPSPILOT_API_KEY:?Set OPSPILOT_API_KEY to an admin API key of your OpsPilot workspace}"

call() {
  local method=$1 path=$2
  shift 2
  curl -fsS -X "$method" "$API$path" -H "Authorization: Bearer $KEY" "$@"
}

echo "Checking OpsPilot at $API ..."
curl -fsS "$API/health/live" > /dev/null || {
  echo "OpsPilot isn't answering at $API. Start it with: uv run uvicorn opspilot.main:app" >&2
  exit 1
}

echo "Connecting Prometheus, Loki and GitHub ..."
call PUT /v1/integrations/prometheus -H "Content-Type: application/json" \
  -d '{"url": "http://localhost:9090"}' > /dev/null
call PUT /v1/integrations/loki -H "Content-Type: application/json" \
  -d '{"url": "http://localhost:3100"}' > /dev/null
call PUT /v1/integrations/github -H "Content-Type: application/json" \
  -d '{"url": "https://api.github.com", "repository": "pranav162074/Kuturl"}' > /dev/null
for kind in prometheus loki github; do
  printf '  %-11s %s\n' "$kind" "$(call POST "/v1/integrations/$kind/check")"
done

echo "Creating a responder key for Alertmanager ..."
created=$(call POST /v1/api-keys -H "Content-Type: application/json" \
  -d '{"name": "alertmanager (demo)", "role": "responder"}')
alert_key=$(printf '%s' "$created" | sed -n 's/.*"key":"\([^"]*\)".*/\1/p')
[ -n "$alert_key" ] || { echo "Couldn't read the new key from: $created" >&2; exit 1; }
printf '%s' "$alert_key" > demo/alertmanager/secrets/opspilot_api_key

echo "Uploading Kuturl's runbooks ..."
for file in demo/runbooks/*.md; do
  call POST /v1/documents -F "file=@$file" -F "source_type=runbook" > /dev/null
  echo "  $file"
done

echo "Recording Kuturl's current deploy ..."
call POST /v1/deploys -H "Content-Type: application/json" -d '{
  "service": "kuturl", "version": "a0ea1ac", "commit_sha": "a0ea1acd6d834a6e80fff403bef86e2271990b6d",
  "description": "kuturl a0ea1ac deployed (demo baseline)", "deployed_by": "demo/setup.sh"
}' > /dev/null

printf 'OPSPILOT_URL=%s\nOPSPILOT_API_KEY=%s\n' "$API" "$KEY" > demo/.demo-env
echo
echo "Done. Runbooks are indexed by the worker (celery ... worker), so keep it running."
echo "Next: bash demo/simulate.sh list"
