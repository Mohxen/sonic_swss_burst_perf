# sonic-swss-burst-perf

This kit measures SWSS pipeline behavior under BGP-like burst route updates in a
KVM SONiC virtual switch. It is designed to separate Redis pressure from
per-event processing pressure in `orchagent`.

## What It Exercises

Preferred path:

```text
route_burst.py -> APPL_DB ProducerStateTable ROUTE_TABLE
              -> orchagent RouteOrch
              -> ASIC_DB
              -> syncd / virtual SAI
```

This bypasses FRR and `fpmsyncd`, but it stresses the SWSS Redis notification
path and `orchagent` per-route processing. Use it first because it is
repeatable and fast.

Optional full path:

```text
FRR/static or BGP burst -> fpmsyncd -> APPL_DB -> orchagent -> ASIC_DB
```

Use the full path after the preferred path if you need to include FRR or
`fpmsyncd` cost.

## Files

- `route_burst.py`: injects add/delete/replace route bursts into `ROUTE_TABLE`.
- `collect_swss_metrics.sh`: samples Redis, Docker CPU, and SWSS logs while a
  burst is running.
- `report_swss_burst.py`: summarizes injector JSONL and metric snapshots.
- `run_burst_matrix.sh`: runs a practical first-pass matrix.

## Prerequisites Inside the SONiC VM

Run as root or with enough permission to access SONiC Docker containers.

Required for representative injection:

```bash
python3 -c 'from swsscommon import swsscommon'
```

Helpful tools:

```bash
redis-cli --version
docker --version
```

If `swsscommon` is missing, install the matching SONiC package for your image.
Do not use the Redis fallback for final measurements because plain Redis writes
do not accurately emulate `ProducerStateTable` notification semantics.

## Quick Start

Copy this directory into the SONiC VM, then run:

```bash
cd tools
mkdir -p results

./collect_swss_metrics.sh results/metrics_$(date +%Y%m%d_%H%M%S) 30 &
COLLECTOR_PID=$!

python3 route_burst.py \
  --count 10000 \
  --batch-size 256 \
  --op add \
  --prefix-base 100.64.0.0 \
  --prefix-len 32 \
  --nexthop 10.0.0.1 \
  --ifname Ethernet0 \
  --jsonl results/inject_add_10k.jsonl

wait "$COLLECTOR_PID"
python3 report_swss_burst.py results
```

Clean up the injected routes:

```bash
python3 route_burst.py \
  --count 10000 \
  --op del \
  --prefix-base 100.64.0.0 \
  --prefix-len 32 \
  --jsonl results/inject_del_10k.jsonl
```

## First-Pass Matrix

Run:

```bash
./run_burst_matrix.sh results/matrix
python3 report_swss_burst.py results/matrix
```

Default matrix:

- 1k, 10k, 50k route adds
- batch sizes 1, 64, 256, 1024
- add followed by delete cleanup

Adjust `COUNT_SET` and `BATCH_SET` environment variables:

```bash
COUNT_SET="1000 10000 100000" BATCH_SET="64 512" ./run_burst_matrix.sh results/matrix_large
```

## Signals To Watch

Redis bottleneck indicators:

- `redis-cli --latency` or `LATENCY LATEST` spikes during injection.
- `SLOWLOG GET` contains `HSET`, `PUBLISH`, `SADD`, `LPUSH`, or Lua calls.
- `database` container CPU is saturated while `orchagent` is not.
- Injection time worsens as batch size increases, but orchagent lag does not.

Per-event `orchagent` bottleneck indicators:

- `orchagent` CPU is saturated while Redis latency remains low.
- APPL_DB pending keys remain high after injection stops.
- ASIC_DB updates trail APPL_DB for a long time.
- Route programming throughput remains flat as Redis batch size increases.
- SWSS logs show repeated retries, unresolved nexthops, or neighbor/interface
  dependency churn.

KVM or virtual SAI bottleneck indicators:

- `syncd` CPU is saturated after `orchagent` has emitted ASIC_DB work.
- Host CPU steal time is high.
- VM disk I/O spikes when Redis persistence is enabled.

## Optimization Checklist

Measure before tuning:

1. Pin VM vCPUs and avoid host overcommit during benchmark runs.
2. Disable unrelated telemetry and management polling if the goal is SWSS
   pipeline isolation.
3. Record SONiC image, kernel, vCPU count, RAM, Redis persistence settings, and
   route scale.
4. Run each matrix point at least three times and compare medians.

Redis-side checks:

1. Confirm APPL_DB Redis persistence policy. For benchmark isolation,
   persistence should normally be disabled or moved off the hot path.
2. Check slowlog during bursts.
3. Compare `batch-size=1` against larger batches. Strong improvement from
   batching suggests Redis round-trip or notification overhead is material.
4. Keep route payloads stable across runs; varying nexthop resolution can hide
   Redis effects.

`orchagent` checks:

1. Test resolved nexthop routes separately from unresolved nexthop routes.
2. Use stable nexthop and interface inputs first. Dependency churn is a
   different workload than route programming.
3. Compare add, replace, and delete. Deletes can expose different object lookup
   and reference-count costs.
4. If source changes are available, instrument `RouteOrch::doTask`,
   `ConsumerStateTable::pops`, and SAI route calls with counters and
   microsecond histograms.

## Suggested Source Instrumentation Points

When you have the `sonic-swss` source tree available, add low-overhead timing
around:

- `ConsumerStateTable::pops`: dequeue count and time per drain.
- `OrchDaemon::start`: select loop wakeups and time spent per executor.
- `RouteOrch::doTask`: number of route events, resolved/unresolved count, total
  processing time.
- SAI route API calls: count, return status, and latency.
- Redis producer path in `fpmsyncd`: routes per FPM message and APPL_DB write
  latency.

Prefer aggregated counters or sampled histograms over per-route log lines.
Per-route logging changes the workload and can become the bottleneck.
