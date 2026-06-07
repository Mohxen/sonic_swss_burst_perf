#!/usr/bin/env bash
set -u

OUT_DIR="${1:-results/orchagent_profile_$(date +%Y%m%d_%H%M%S)}"
PROFILE_SECONDS="${PROFILE_SECONDS:-90}"
COUNT="${COUNT:-10000}"
BATCH_SIZE="${BATCH_SIZE:-64}"
PREFIX_BASE="${PREFIX_BASE:-100.64.0.0}"
PREFIX_LEN="${PREFIX_LEN:-32}"
NEXTHOP="${NEXTHOP:-10.0.0.1}"
IFNAME="${IFNAME:-Ethernet0}"
PERF_FREQ="${PERF_FREQ:-99}"
WAIT_AFTER_ADD="${WAIT_AFTER_ADD:-1}"
DRAIN_TIMEOUT="${DRAIN_TIMEOUT:-300}"
DRAIN_INTERVAL="${DRAIN_INTERVAL:-2}"

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

ORCH_PID="$(pgrep -f '/usr/bin/orchagent' | head -1)"
if [ -z "$ORCH_PID" ]; then
    echo "could not find orchagent process" >&2
    exit 2
fi
if ! command -v perf >/dev/null 2>&1; then
    echo "perf is not installed in this SONiC image" >&2
    exit 2
fi

{
    echo "timestamp=$(date -Is)"
    echo "orchagent_pid=$ORCH_PID"
    echo "profile_seconds=$PROFILE_SECONDS"
    echo "perf_freq=$PERF_FREQ"
    echo "count=$COUNT"
    echo "batch_size=$BATCH_SIZE"
    echo "prefix_base=$PREFIX_BASE"
    echo "prefix_len=$PREFIX_LEN"
    echo "nexthop=$NEXTHOP"
    echo "ifname=$IFNAME"
    echo "pending_before=$(pending_routes)"
    ip neigh show "$NEXTHOP" 2>/dev/null || true
    docker exec swss sh -c "ps -eo pid,ppid,pcpu,pmem,comm,args | grep -E 'orchagent|PID' | grep -v grep" 2>/dev/null || true
} > "$OUT_DIR/profile.log"

docker cp swss:/usr/bin/orchagent "$OUT_DIR/orchagent.binary" >/dev/null 2>&1 || true
if [ -f "$OUT_DIR/orchagent.binary" ]; then
    file "$OUT_DIR/orchagent.binary" > "$OUT_DIR/orchagent.file" 2>&1 || true
    readelf -n "$OUT_DIR/orchagent.binary" > "$OUT_DIR/orchagent.notes" 2>&1 || true
    readelf -x .gnu_debuglink "$OUT_DIR/orchagent.binary" > "$OUT_DIR/orchagent.debuglink" 2>&1 || true
fi

sudo perf record \
    -F "$PERF_FREQ" \
    -g \
    -p "$ORCH_PID" \
    -o "$OUT_DIR/perf.data" \
    -- sleep "$PROFILE_SECONDS" \
    > "$OUT_DIR/perf_record.stdout" \
    2> "$OUT_DIR/perf_record.stderr" &
PERF_PID=$!

python3 route_burst.py \
    --count "$COUNT" \
    --batch-size "$BATCH_SIZE" \
    --op add \
    --prefix-base "$PREFIX_BASE" \
    --prefix-len "$PREFIX_LEN" \
    --nexthop "$NEXTHOP" \
    --ifname "$IFNAME" \
    --jsonl "$OUT_DIR/inject_add.jsonl"

echo "pending_after_add=$(pending_routes)" >> "$OUT_DIR/profile.log"
wait "$PERF_PID"

sudo perf report --stdio -i "$OUT_DIR/perf.data" --sort symbol,dso \
    > "$OUT_DIR/perf_report_top.txt" 2> "$OUT_DIR/perf_report.stderr" || true
sudo perf report --stdio -i "$OUT_DIR/perf.data" --no-children --sort dso,symbol \
    > "$OUT_DIR/perf_report_flat.txt" 2> "$OUT_DIR/perf_report_flat.stderr" || true

if [ "$WAIT_AFTER_ADD" = "1" ]; then
    wait_for_pending_zero "$OUT_DIR/drain_wait_add.csv" "$DRAIN_TIMEOUT" || true
fi

python3 route_burst.py \
    --count "$COUNT" \
    --batch-size "$BATCH_SIZE" \
    --op del \
    --prefix-base "$PREFIX_BASE" \
    --prefix-len "$PREFIX_LEN" \
    --nexthop "$NEXTHOP" \
    --ifname "$IFNAME" \
    --jsonl "$OUT_DIR/inject_del.jsonl"

wait_for_pending_zero "$OUT_DIR/drain_wait_del.csv" "$DRAIN_TIMEOUT" || true
echo "pending_after_delete=$(pending_routes)" >> "$OUT_DIR/profile.log"

echo "profile written to $OUT_DIR"
sed -n '1,80p' "$OUT_DIR/perf_report_flat.txt"
