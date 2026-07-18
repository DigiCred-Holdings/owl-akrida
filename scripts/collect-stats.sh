#!/usr/bin/env bash
# Sample docker stats and PostgreSQL activity while a benchmark run is active.
set -euo pipefail

PROJECT="${COMPOSE_PROJECT_NAME:-owl-benchmark}"
OUT_DIR="${1:?usage: collect-stats.sh <output-dir> [duration-seconds]}"
DURATION="${2:-900}"
INTERVAL="${STATS_INTERVAL:-2}"

mkdir -p "$OUT_DIR"
STATS_CSV="$OUT_DIR/docker-stats.csv"
PG_CSV="$OUT_DIR/postgres-activity.csv"
META="$OUT_DIR/collector-meta.txt"

echo "project=$PROJECT duration=$DURATION interval=$INTERVAL started=$(date -Is)" >"$META"

echo "timestamp,container,cpu_perc,mem_usage,mem_perc,net_io,block_io,pids" >"$STATS_CSV"
echo "timestamp,total_connections,active,idle,waiting,max_connections" >"$PG_CSV"

ISSUER_DB_CID="$(docker compose -p "$PROJECT" ps -q issuer-db 2>/dev/null || true)"
end=$((SECONDS + DURATION))

while (( SECONDS < end )); do
  ts="$(date -Is)"
  docker stats --no-stream --format \
    "{{.Name}},{{.CPUPerc}},{{.MemUsage}},{{.MemPerc}},{{.NetIO}},{{.BlockIO}},{{.PIDs}}" \
    $(docker compose -p "$PROJECT" ps -q 2>/dev/null) 2>/dev/null \
    | while IFS= read -r line; do
        echo "$ts,$line" >>"$STATS_CSV"
      done || true

  if [[ -n "$ISSUER_DB_CID" ]]; then
    docker exec "$ISSUER_DB_CID" psql -U test -d postgres -At -F',' -c \
      "SELECT now(),
              (SELECT count(*) FROM pg_stat_activity WHERE usename = 'test' OR datname IS NOT NULL),
              (SELECT count(*) FROM pg_stat_activity WHERE state = 'active'),
              (SELECT count(*) FROM pg_stat_activity WHERE state = 'idle'),
              (SELECT count(*) FROM pg_stat_activity WHERE wait_event IS NOT NULL AND state != 'idle'),
              (SELECT setting FROM pg_settings WHERE name = 'max_connections');" \
      >>"$PG_CSV" 2>/dev/null || true
  fi

  sleep "$INTERVAL"
done

echo "finished=$(date -Is)" >>"$META"
