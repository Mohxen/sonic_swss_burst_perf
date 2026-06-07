#!/usr/bin/env bash
set -u

OUT_ROOT="${1:-results/adaptive_comparison}"
COUNT_SET="${COUNT_SET:-20000 30000 50000}"
BATCH_SET="${BATCH_SET:-64}"
PREFIX_BASE="${PREFIX_BASE:-100.64.0.0}"
PREFIX_LEN="${PREFIX_LEN:-32}"
NEXTHOP="${NEXTHOP:-10.0.0.1}"
IFNAME="${IFNAME:-Ethernet0}"
METRIC_DURATION="${METRIC_DURATION:-300}"
DRAIN_TIMEOUT="${DRAIN_TIMEOUT:-600}"
DRAIN_INTERVAL="${DRAIN_INTERVAL:-2}"

ADAPTIVE_TARGET_PENDING="${ADAPTIVE_TARGET_PENDING:-10000}"
ADAPTIVE_HIGH_PENDING="${ADAPTIVE_HIGH_PENDING:-20000}"
ADAPTIVE_LOW_PENDING="${ADAPTIVE_LOW_PENDING:-5000}"
ADAPTIVE_MIN_BATCH_SIZE="${ADAPTIVE_MIN_BATCH_SIZE:-32}"
ADAPTIVE_MAX_BATCH_SIZE="${ADAPTIVE_MAX_BATCH_SIZE:-512}"
ADAPTIVE_SLEEP_MS_MIN="${ADAPTIVE_SLEEP_MS_MIN:-0}"
ADAPTIVE_SLEEP_MS_MAX="${ADAPTIVE_SLEEP_MS_MAX:-500}"
ADAPTIVE_PENDING_CHECK_INTERVAL="${ADAPTIVE_PENDING_CHECK_INTERVAL:-1}"

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

run_one_mode() {
    mode="$1"
    count="$2"
    batch="$3"
    run_dir="$4"
    mkdir -p "$run_dir"

    echo "running mode=$mode count=$count batch=$batch"
    ./collect_swss_metrics.sh "$run_dir/metrics" "$METRIC_DURATION" &
    collector_pid=$!

    if [ "$mode" = "adaptive" ]; then
        python3 adaptive_route_burst.py \
            --count "$count" \
            --batch-size "$batch" \
            --op add \
            --prefix-base "$PREFIX_BASE" \
            --prefix-len "$PREFIX_LEN" \
            --nexthop "$NEXTHOP" \
            --ifname "$IFNAME" \
            --target-pending "$ADAPTIVE_TARGET_PENDING" \
            --high-pending "$ADAPTIVE_HIGH_PENDING" \
            --low-pending "$ADAPTIVE_LOW_PENDING" \
            --min-batch-size "$ADAPTIVE_MIN_BATCH_SIZE" \
            --max-batch-size "$ADAPTIVE_MAX_BATCH_SIZE" \
            --sleep-ms-min "$ADAPTIVE_SLEEP_MS_MIN" \
            --sleep-ms-max "$ADAPTIVE_SLEEP_MS_MAX" \
            --pending-check-interval "$ADAPTIVE_PENDING_CHECK_INTERVAL" \
            --jsonl "$run_dir/inject_add.jsonl"
    else
        python3 route_burst.py \
            --count "$count" \
            --batch-size "$batch" \
            --op add \
            --prefix-base "$PREFIX_BASE" \
            --prefix-len "$PREFIX_LEN" \
            --nexthop "$NEXTHOP" \
            --ifname "$IFNAME" \
            --jsonl "$run_dir/inject_add.jsonl"
    fi

    wait_for_pending_zero "$run_dir/drain_wait_add.csv" "$DRAIN_TIMEOUT"
    drain_status=$?
    wait "$collector_pid"

    if [ "$mode" = "adaptive" ]; then
        python3 adaptive_route_burst.py \
            --count "$count" \
            --batch-size "$batch" \
            --op del \
            --prefix-base "$PREFIX_BASE" \
            --prefix-len "$PREFIX_LEN" \
            --nexthop "$NEXTHOP" \
            --ifname "$IFNAME" \
            --target-pending "$ADAPTIVE_TARGET_PENDING" \
            --high-pending "$ADAPTIVE_HIGH_PENDING" \
            --low-pending "$ADAPTIVE_LOW_PENDING" \
            --min-batch-size "$ADAPTIVE_MIN_BATCH_SIZE" \
            --max-batch-size "$ADAPTIVE_MAX_BATCH_SIZE" \
            --sleep-ms-min "$ADAPTIVE_SLEEP_MS_MIN" \
            --sleep-ms-max "$ADAPTIVE_SLEEP_MS_MAX" \
            --pending-check-interval "$ADAPTIVE_PENDING_CHECK_INTERVAL" \
            --jsonl "$run_dir/inject_del.jsonl"
    else
        python3 route_burst.py \
            --count "$count" \
            --batch-size "$batch" \
            --op del \
            --prefix-base "$PREFIX_BASE" \
            --prefix-len "$PREFIX_LEN" \
            --nexthop "$NEXTHOP" \
            --ifname "$IFNAME" \
            --jsonl "$run_dir/inject_del.jsonl"
    fi

    wait_for_pending_zero "$run_dir/drain_wait_del.csv" "$DRAIN_TIMEOUT" || true
    if [ "$drain_status" != "0" ]; then
        echo "warning: add pending queue did not fully drain before delete for mode=$mode count=$count batch=$batch"
    fi
}

for count in $COUNT_SET; do
    for batch in $BATCH_SET; do
        run_root="$OUT_ROOT/count_${count}_batch_${batch}"
        run_one_mode normal "$count" "$batch" "$run_root/normal"
        sleep 5
        run_one_mode adaptive "$count" "$batch" "$run_root/adaptive"
        sleep 5
    done
done

python3 report_swss_burst.py "$OUT_ROOT"
