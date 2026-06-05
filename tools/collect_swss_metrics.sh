#!/usr/bin/env bash
set -u

OUT_DIR="${1:-results/metrics}"
DURATION_SECONDS="${2:-60}"
INTERVAL_SECONDS="${INTERVAL_SECONDS:-1}"

mkdir -p "$OUT_DIR"

run_redis() {
    if command -v sonic-db-cli >/dev/null 2>&1; then
        sonic-db-cli APPL_DB "$@"
    elif command -v redis-cli >/dev/null 2>&1; then
        redis-cli -n 0 "$@"
    elif command -v docker >/dev/null 2>&1; then
        docker exec database redis-cli -n 0 "$@"
    else
        return 127
    fi
}

START_EPOCH="$(date +%s)"
END_EPOCH="$((START_EPOCH + DURATION_SECONDS))"

{
    echo "timestamp,appl_db_keys,route_pending_keys"
    while [ "$(date +%s)" -lt "$END_EPOCH" ]; do
        TS="$(date +%s)"
        DBSIZE="$(run_redis DBSIZE 2>/dev/null | tr -d '\r' || true)"
        PENDING="$(run_redis SCARD ROUTE_TABLE_KEY_SET 2>/dev/null | tr -d '\r' || true)"
        echo "$TS,${DBSIZE:-na},${PENDING:-na}"
        sleep "$INTERVAL_SECONDS"
    done
} > "$OUT_DIR/redis_appldb_samples.csv" &
SAMPLE_PID=$!

{
    while [ "$(date +%s)" -lt "$END_EPOCH" ]; do
        echo "### $(date -Is)"
        run_redis INFO commandstats 2>&1 || true
        run_redis LATENCY LATEST 2>&1 || true
        run_redis SLOWLOG GET 32 2>&1 || true
        sleep "$INTERVAL_SECONDS"
    done
} > "$OUT_DIR/redis_diagnostics.log" &
REDIS_DIAG_PID=$!

if command -v docker >/dev/null 2>&1; then
    {
        while [ "$(date +%s)" -lt "$END_EPOCH" ]; do
            echo "### $(date -Is)"
            docker stats --no-stream database swss syncd bgp 2>&1 || true
            sleep "$INTERVAL_SECONDS"
        done
    } > "$OUT_DIR/docker_stats.log" &
    DOCKER_PID=$!
else
    DOCKER_PID=""
fi

if [ -r /var/log/syslog ]; then
    tail -n 0 -F /var/log/syslog > "$OUT_DIR/syslog_tail.log" &
    SYSLOG_PID=$!
else
    SYSLOG_PID=""
fi

wait "$SAMPLE_PID"
wait "$REDIS_DIAG_PID"
if [ -n "${DOCKER_PID:-}" ]; then
    wait "$DOCKER_PID"
fi
if [ -n "${SYSLOG_PID:-}" ]; then
    kill "$SYSLOG_PID" >/dev/null 2>&1 || true
fi

echo "metrics written to $OUT_DIR"

