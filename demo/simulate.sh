#!/usr/bin/env bash
# The incident simulator: breaks Kuturl in known ways, so OpsPilot's investigations can be
# checked against a cause we know. Every run is appended to demo/simulations.jsonl with its
# true root cause, the ground truth for evaluating the agent later.
#
#   bash demo/simulate.sh list
#   bash demo/simulate.sh redis-down [seconds]
#   bash demo/simulate.sh db-lock [seconds]
#   bash demo/simulate.sh traffic-flood [seconds]
#   bash demo/simulate.sh bad-deploy [seconds]
#   bash demo/simulate.sh restore          # puts everything back to normal
set -euo pipefail
cd "$(dirname "$0")/.."

[ -f demo/.demo-env ] && . demo/.demo-env
compose() { docker compose --profile demo "$@"; }
now() { date -u +%Y-%m-%dT%H:%M:%SZ; }

record() { # scenario, root cause, started_at
  printf '{"scenario":"%s","root_cause":"%s","started_at":"%s","ended_at":"%s"}\n' \
    "$1" "$2" "$3" "$(now)" >> demo/simulations.jsonl
}

record_deploy() { # version, description
  if [ -z "${OPSPILOT_API_KEY:-}" ]; then
    echo "  (OPSPILOT_API_KEY not set: run demo/setup.sh first to record deploys)"
    return
  fi
  curl -fsS -X POST "${OPSPILOT_URL:-http://localhost:8000}/v1/deploys" \
    -H "Authorization: Bearer $OPSPILOT_API_KEY" -H "Content-Type: application/json" \
    -d "{\"service\":\"kuturl\",\"version\":\"$1\",\"description\":\"$2\",\"deployed_by\":\"demo/simulate.sh\"}" \
    > /dev/null && echo "  recorded deploy $1 in OpsPilot"
}

countdown() {
  local seconds=$1
  echo "  holding for ${seconds}s (alerts fire after 30-60s; Ctrl+C restores early)"
  trap 'echo; restore; exit 130' INT
  sleep "$seconds"
  trap - INT
}

restore() {
  echo "Restoring Kuturl ..."
  compose start kuturl-redis > /dev/null 2>&1 || true
  KUTURL_DB_PASSWORD=kuturl compose up -d kuturl > /dev/null 2>&1 || true
  compose exec -T kuturl-postgres psql -U kuturl -d kuturl -qc \
    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE application_name = 'opspilot-simulator';" \
    > /dev/null 2>&1 || true
  echo "  done; alerts resolve within a minute or two"
}

scenario=${1:-list}
seconds=${2:-180}
started=$(now)

case "$scenario" in
  list)
    cat <<'LIST'
Scenarios (each holds for 180s by default, then restores itself):
  redis-down      Kuturl's Redis stops. Cache, rate limits and the click stream fail.
                  Expect: KuturlRedisDown (high) -> incident -> automatic investigation.
  db-lock         A long transaction locks the links table. Database queries queue up.
                  Expect: KuturlDatabaseLocked, then KuturlHighLatency.
  traffic-flood   200 concurrent connections hammer the redirect endpoint.
                  Expect: KuturlTrafficSpike, and latency rising.
  bad-deploy      A new Kuturl version ships with a wrong database password and crash-loops.
                  Expect: KuturlDown (critical); the deploy shows up in recent changes.
  restore         Put everything back.
LIST
    ;;
  redis-down)
    echo "Stopping Kuturl's Redis ..."
    compose stop kuturl-redis > /dev/null
    countdown "$seconds"
    restore
    record redis-down "Kuturl's Redis container was stopped" "$started"
    ;;
  db-lock)
    echo "Locking the links table in an open transaction ..."
    compose exec -T -e PGAPPNAME=opspilot-simulator kuturl-postgres psql -U kuturl -d kuturl -qc \
      "BEGIN; LOCK TABLE links IN ACCESS EXCLUSIVE MODE; SELECT pg_sleep($seconds); COMMIT;" \
      > /dev/null 2>&1 &
    countdown "$seconds"
    restore
    record db-lock "A long transaction held an ACCESS EXCLUSIVE lock on the links table" "$started"
    ;;
  traffic-flood)
    echo "Flooding Kuturl with 200 concurrent connections ..."
    code=$(compose exec -T traffic node -e \
      "console.log(JSON.parse(require('fs').readFileSync('/state/traffic.json','utf8')).codes[0])")
    compose exec -T traffic npx autocannon -c 200 -d "$seconds" --no-progress \
      "http://kuturl:3000/$code" > /dev/null 2>&1 || true
    record traffic-flood "A traffic flood: 200 concurrent connections to one short link" "$started"
    echo "  flood over"
    ;;
  bad-deploy)
    record_deploy "v2-bad-config" "kuturl v2: new database credentials"
    echo "Deploying Kuturl with a wrong database password ..."
    KUTURL_DB_PASSWORD=wrong-password compose up -d kuturl > /dev/null
    countdown "$seconds"
    restore
    record_deploy "v2-rollback" "kuturl rolled back to the previous configuration"
    record bad-deploy "A deploy changed Kuturl's database password to a wrong one" "$started"
    ;;
  restore)
    restore
    ;;
  *)
    echo "Unknown scenario: $scenario (try: bash demo/simulate.sh list)" >&2
    exit 1
    ;;
esac
