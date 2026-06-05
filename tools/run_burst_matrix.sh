#!/usr/bin/env bash
set -u

OUT_ROOT="${1:-results/matrix_$(date +%Y%m%d_%H%M%S)}"
COUNT_SET="${COUNT_SET:-1000 10000 50000}"
BATCH_SET="${BATCH_SET:-1 64 256 1024}"
PREFIX_BASE="${PREFIX_BASE:-100.64.0.0}"
PREFIX_LEN="${PREFIX_LEN:-32}"
NEXTHOP="${NEXTHOP:-10.0.0.1}"
IFNAME="${IFNAME:-Ethernet0}"
METRIC_DURATION="${METRIC_DURATION:-45}"
WAIT_FOR_DRAIN="${WAIT_FOR_DRAIN:-1}"
DRAIN_TIMEOUT="${DRAIN_TIMEOUT:-300}"
DRAIN_INTERVAL="${DRAIN_INTERVAL:-2}"

mkdir -p "$OUT_ROOT"

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

pending_routes() {
    run_redis SCARD ROUTE_TABLE_KEY_SET 2>/dev/null | tr -d '\r' || true
}

wait_for_pending_zero() {
    out_file="$1"
    timeout="$2"
    start_epoch="$(date +%s)"
    deadline="$((start_epoch + timeout))"
    echo "timestamp,elapsed_seconds,route_pending_keys" > "$out_file"

    while :; do
        now="$(date +%s)"
        pending="$(pending_routes)"
        echo "$now,$((now - start_epoch)),${pending:-na}" >> "$out_file"

        if [ "$pending" = "0" ]; then
            echo "pending routes drained in $((now - start_epoch))s"
            return 0
        fi

        if [ "$now" -ge "$deadline" ]; then
            echo "pending routes did not drain within ${timeout}s; last_pending=${pending:-na}"
            return 1
        fi

        sleep "$DRAIN_INTERVAL"
    done
}

for count in $COUNT_SET; do
    for batch in $BATCH_SET; do
        RUN_DIR="$OUT_ROOT/count_${count}_batch_${batch}"
        mkdir -p "$RUN_DIR"
        echo "running count=$count batch=$batch"

        ./collect_swss_metrics.sh "$RUN_DIR/metrics" "$METRIC_DURATION" &
        COLLECTOR_PID=$!

        python3 route_burst.py \
            --count "$count" \
            --batch-size "$batch" \
            --op add \
            --prefix-base "$PREFIX_BASE" \
            --prefix-len "$PREFIX_LEN" \
            --nexthop "$NEXTHOP" \
            --ifname "$IFNAME" \
            --jsonl "$RUN_DIR/inject_add.jsonl"

        if [ "$WAIT_FOR_DRAIN" = "1" ]; then
            wait_for_pending_zero "$RUN_DIR/drain_wait_add.csv" "$DRAIN_TIMEOUT"
            DRAIN_STATUS=$?
        else
            DRAIN_STATUS=0
        fi

        wait "$COLLECTOR_PID"

        python3 route_burst.py \
            --count "$count" \
            --batch-size "$batch" \
            --op del \
            --prefix-base "$PREFIX_BASE" \
            --prefix-len "$PREFIX_LEN" \
            --nexthop "$NEXTHOP" \
            --ifname "$IFNAME" \
            --jsonl "$RUN_DIR/inject_del.jsonl"

        if [ "$WAIT_FOR_DRAIN" = "1" ]; then
            wait_for_pending_zero "$RUN_DIR/drain_wait_del.csv" "$DRAIN_TIMEOUT" || true
        fi

        if [ "$DRAIN_STATUS" != "0" ]; then
            echo "warning: add pending queue did not fully drain before delete for count=$count batch=$batch"
        fi

        sleep 5
    done
done

python3 report_swss_burst.py "$OUT_ROOT"
