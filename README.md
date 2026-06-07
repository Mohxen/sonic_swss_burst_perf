# sonic-swss-burst-perf

Performance investigation toolkit for SONiC SWSS orchagent under burst route updates, running in a KVM SONiC virtual switch.

The workload emulates a BGP-like route burst by writing route add/delete events into APPL\_DB `ROUTE_TABLE` via `swsscommon.ProducerStateTable` (the same path real BGP uses), then measuring how Redis, SWSS/orchagent, and downstream virtual SAI/syncd behave while the queue drains.

---

## Key Finding

**`swss::tokenize()` uses `std::istringstream` — which constructs a `std::locale` on every call — and is invoked 10+ times per route in `RouteOrch::doTask()`. At 50 000 routes this causes 500 000 locale initialisations in the hot path.**

Replacing `istringstream` with a plain `find`/`substr` loop (no rebuild needed — applied via `LD_PRELOAD`) reduced 50k-route drain time from **190 seconds to under 1 second: a >190× improvement.**

| Routes | Before | After |
|---:|---:|---:|
| 10k | 4 s | <1 s |
| 20k | 25 s | <1 s |
| 30k | 61 s | <1 s |
| 40k | 117 s | <1 s |
| 50k | **190 s** | **<1 s** |

The fix is in [`tools/tokenize_fix.cpp`](tools/tokenize_fix.cpp). The full investigation that led here is documented below.

---

## Repository Contents

| Path | Purpose |
|---|---|
| `tools/` | Injection, collection, profiling, and reporting scripts |
| `results/` | Captured benchmark runs from the SONiC VS VM |
| `charts/` | Generated CSV/SVG summaries |
| `logs/` | VM serial log captured during experiments |

---

## Investigation Journey

### Phase 1 — Initial measurement (what showed the problem)

**Tools used:** `route_burst.py`, `run_burst_matrix.sh`, `collect_swss_metrics.sh`

First runs used batch-size 64 at 10k and 50k routes. The injector flooded Redis as fast as possible; metrics polled the pending queue depth every 3 seconds.

**Key observation:** injection completed quickly (~4–23 s) but the pending queue drained slowly and non-linearly.

| Count | Inject time | Add drain | Peak pending |
|---|---:|---:|---:|
| 10k, batch 64 | 4.7 s | 2 s | 1 741 |
| 50k, batch 64 | 23.4 s | **236 s** | 33 116 |

The drain scaled ~118× while route count scaled only 5×. Docker stats showed high `swss` CPU, low `syncd` CPU — pointing to orchagent route-event processing as the bottleneck, not SAI or Redis.

**Scaling knee** (from `results/knee_b64/`): add drain jumped from 22 s at 20k to 65 s at 30k and 132 s at 40k.

> **Methodology note:** these initial runs were done with an unresolved next-hop (`10.0.0.1` had no ARP entry). orchagent defers unresolved routes immediately, which produces a very fast injection rate (~2 100 routes/s) but may change the drain behaviour. The bottleneck identification (orchagent, not syncd/Redis) remains valid regardless of neighbor state.

---

### Phase 2 — Deeper investigation

#### 2a. Adaptive back-pressure injection

**Tool:** `tools/adaptive_route_burst.py`, `tools/run_adaptive_comparison.sh`

Normal injection floods Redis regardless of how fast orchagent drains. The adaptive injector reads `ROUTE_TABLE_KEY_SET` after each batch and adjusts batch size and sleep time to keep the pending queue below a target depth.

Results from `results/adaptive_comparison_b64/` (resolved neighbor):

| Count | Normal drain | Adaptive drain | Improvement |
|---|---:|---:|---|
| 10k | 112 s | 83 s | 26% |
| 20k | 106 s | **20 s** | **81%** |
| 30k | 60 s | 46 s | 23% |

Adaptive helped but results were inconsistent across runs, indicating the VM state (neighbor resolution, orchagent warm-up) was a significant factor.

#### 2b. Resolved vs unresolved neighbor

**Results:** `results/resolved_10k_b64/`, `results/unresolved_10k_b64/`

With a **resolved** (PERMANENT) neighbor, orchagent programs each route into the ASIC immediately. With an **unresolved** neighbor, orchagent defers routes waiting for ARP — completely different code path.

All subsequent measurements used a resolved (PERMANENT) neighbor to measure the real production path:

```bash
sudo ip neigh replace 10.0.0.1 dev Ethernet0 lladdr 02:00:00:00:00:01 nud permanent
```

#### 2c. CPU profiling with perf

**Tool:** `tools/profile_orchagent_perf.sh`

`perf record -g` was attached to the orchagent PID during a 10k route burst. The orchagent binary is fully stripped (no debug symbols), so function names cannot be resolved directly. However, the call chain samples revealed the hot library operations:

```
33%  [unknown orchagent code]
      ├─ 5.6%  0x5636b1568743   (orchagent — stripped)
      ├─ 5.4%  0x5636b156880f   (orchagent — stripped)
      ├─ 2.8%  std::vector<string>::_M_realloc_append   ← no .reserve() before push_back
      ├─ 1.7%  std::locale::id::_M_id()                 ← locale init on every call
      ├─ 1.0%  std::basic_ios::_M_cache_locale           ← same
      └─ 0.8%  std::getline                              ← reading from istringstream
```

The `locale::id::_M_id` + `_M_cache_locale` + `getline` cluster is the signature of `std::istringstream` being constructed and used. This pointed directly to `swss::tokenize()`.

#### 2d. Syscall analysis with strace

**Results:** `results/strace_orchagent_syscalls_10k_*/`

`strace -c` (syscall accounting, no function names needed, works on stripped binaries) during a 10k burst:

| Syscall | % time | calls | notes |
|---|---:|---:|---|
| `poll` | 88% | 393 | orchagent event loop idle time |
| `newfstatat` | 6.4% | 10 097 | **exactly 1 per route** — `/etc/localtime` |
| `write` | 5.4% | 10 100 | syslog writes |

Every route triggered one `stat("/etc/localtime")` call from the syslog infrastructure. Switching log level to ERROR reduced inject/drain time by ~20% but did not eliminate the stat calls (even ERROR-level messages trigger syslog).

---

### Phase 3 — Root cause identified in source code

The source for the running SONiC build was available at `/home/admin/symbol-build/sonic-swss-common/common/tokenize.cpp`. The implementation:

```cpp
// Original tokenize() — sonic-swss-common/common/tokenize.cpp
vector<string> tokenize(const string &str, const char token)
{
    string tmp;
    vector<string> ret;
    istringstream iss(str);          // ← constructs locale on EVERY call
    while (getline(iss, tmp, token)) // ← reads through locale machinery
        ret.push_back(tmp);          // ← no reserve: triggers realloc
    return ret;
}
```

`std::istringstream` construction initialises `std::locale`, which acquires a global mutex and performs multiple heap allocations. This is called from `RouteOrch::doTask()` in `orchagent/routeorch.cpp`:

```cpp
// routeorch.cpp lines 848-855 — called for every SET route event
ipv      = tokenize(ips,           ',');   // nexthop IPs
alsv     = tokenize(aliases,       ',');   // interface names
mpls_nhv = tokenize(mpls_nhs,      ',');
vni_labelv = tokenize(vni_labels,  ',');
rmacv    = tokenize(remote_macs,   ',');
srv6_segv = tokenize(srv6_segments,',');
srv6_src  = tokenize(srv6_source,  ',');
srv6_vpn_sidv = tokenize(srv6_vpn_sids, ',');
```

Then `NextHopGroupKey` and `NextHopKey` constructors each call `tokenize()` 1–2 more times during key parsing (`nexthopgroupkey.h:18,62`, `nexthopkey.h:57`).

**Total: ≥10 `istringstream` constructions per route.**

At 50 000 routes: **500 000 locale initialisations** in the hot path.

---

### Phase 4 — Fix applied and verified

**Fix:** `tools/tokenize_fix.cpp`

Replace `istringstream` with a plain `find`/`substr` loop and pre-reserve the output vector:

```cpp
// Fixed tokenize() — no istringstream, no locale, pre-reserved
vector<string> tokenize(const string &str, const char token)
{
    vector<string> ret;
    if (str.empty()) return ret;
    ret.reserve(4);
    size_t start = 0, pos;
    while ((pos = str.find(token, start)) != string::npos) {
        ret.push_back(str.substr(start, pos - start));
        start = pos + 1;
    }
    ret.push_back(str.substr(start));
    return ret;
}
```

Applied via `LD_PRELOAD` (no rebuild required — the symbol is exported from `libswsscommon.so`):

```bash
g++ -O2 -fPIC -std=c++17 -shared -o fast_tokenize.so tools/tokenize_fix.cpp
docker cp fast_tokenize.so swss:/usr/local/lib/fast_tokenize.so
# add  export LD_PRELOAD=/usr/local/lib/fast_tokenize.so  before exec in orchagent.sh
docker exec swss supervisorctl restart orchagent
```

---

## Key Results: Before and After

### Add drain time — resolved neighbor, batch size 64

| Count | Phase 1 baseline (unresolved) | Phase 2 baseline (resolved) | **With tokenize fix** |
|---|---:|---:|---:|
| 10k | 2 s | 4 s | **<1 s** |
| 20k | 22 s | 25 s | **<1 s** |
| 30k | 65 s | 61 s | **<1 s** |
| 40k | 132 s | 117 s | **<1 s** |
| 50k | 236 s | 190 s | **<1 s** |

### Peak pending queue depth

| Count | Baseline (resolved) | With tokenize fix |
|---|---:|---:|
| 10k | 1 350 | **2** |
| 20k | 8 515 | **1** |
| 30k | 15 953 | **2** |
| 40k | 24 244 | **4** |
| 50k | 31 439 | **247** |

**The tokenize fix reduced 50k add drain time from 190 s to <1 s — a >190× improvement.** After the fix, orchagent processes routes faster than the injector can produce them. The pending queue stays near zero throughout the entire injection.

---

## Remaining Opportunities

After the tokenize fix the new bottleneck is **Redis ProducerStateTable write throughput** (~1 100 routes/s at batch 64). Orchagent is no longer the bottleneck.

Remaining orchagent improvements (diminishing returns now):

| Fix | Location | What it removes |
|---|---|---|
| nhg\_str round-trip | `routeorch.cpp:975–984` | Build string from vectors then immediately re-parse it in `NextHopGroupKey` |
| Cache `m_syncdRoutes` iterator | `routeorch.cpp:1068–1076` | 3 redundant O(log N) map lookups reduced to 1 per route |
| `TZ=UTC` env var | swss container | 10k `stat("/etc/localtime")` calls per 10k routes |
| Switch to `unordered_map` | `routeorch.h:119` | O(log N) → O(1) per-route lookup — matters at 100k+ routes |

---

## Tools Reference

| Tool | What it does |
|---|---|
| `route_burst.py` | Injects N routes via `swsscommon.ProducerStateTable` (same path as real BGP) |
| `adaptive_route_burst.py` | Like above, throttles injection based on live pending queue depth |
| `collect_swss_metrics.sh` | Polls `ROUTE_TABLE_KEY_SET` depth + docker CPU every ~3 s |
| `run_burst_matrix.sh` | Outer loop over `COUNT_SET × BATCH_SET` — normal mode |
| `run_adaptive_comparison.sh` | Runs normal + adaptive back-to-back per (count, batch) pair |
| `profile_orchagent_perf.sh` | Attaches `perf record -g` to orchagent PID during a burst |
| `report_swss_burst.py` | Summarizes all result dirs — reads JSONL + CSV, prints tables |
| `plot_pending_timeline.py` | Generates SVG charts of pending queue depth over time |
| `tokenize_fix.cpp` | Replacement `swss::tokenize()` — apply via LD\_PRELOAD |

---

## Results Directory Map

| Directory | Phase | Neighbor | What it measured |
|---|---|---|---|
| `clean_10k_b64/` | 1 | unresolved | Initial 10k baseline |
| `clean_50k_b64/` | 1 | unresolved | Initial 50k baseline |
| `baseline_50k_b64/` | 1 | unresolved | Early 50k experiment |
| `drain_50k/`, `drain_50k_300s/` | 1 | unresolved | Drain wait experiments |
| `matrix/` | 1 | unresolved | 1k/10k/50k × batch 1/64/256/1024 |
| `knee_b64/` | 1 | unresolved | 20k/30k/40k — finding the scaling knee |
| `smoke/` | 1 | n/a | 100-route sanity check |
| `unresolved_10k_b64/` | 2 | ❌ unresolved | Unresolved neighbor reference |
| `resolved_10k_b64/` | 2 | ✅ resolved | Resolved neighbor reference |
| `adaptive_comparison_b64/` | 2 | ✅ resolved | Normal vs adaptive at 10k/20k/30k |
| `resolved_adaptive_10k_b64/` | 2 | ✅ resolved | Adaptive detail at 10k |
| `resolved_adaptive_20k_b64/` | 2 | ✅ resolved | Adaptive detail at 20k |
| `strace_orchagent_syscalls_10k_*/` | 2 | unresolved | Syscall profile at NOTICE/ERROR log level |
| `orchagent_cpu_10k_*/` | 2 | both | perf call-graph during burst |
| `orchagent_profile_10k_resolved/` | 2 | ✅ resolved | perf profile — resolved neighbor |
| `scale_5x_baseline/` | 3 | ✅ resolved | **Corrected baseline** — 10k–50k, unpatched |
| `scale_5x_fixed/` | 3 | ✅ resolved | **After tokenize fix** — 10k–50k, drain <1 s |

---

## How to Reproduce

### Baseline measurement

```bash
# Inside the SONiC VM — ensure neighbor is resolved first
sudo ip neigh replace 10.0.0.1 dev Ethernet0 lladdr 02:00:00:00:00:01 nud permanent

COUNT_SET="10000 20000 30000 40000 50000" BATCH_SET="64" DRAIN_TIMEOUT=600 \
  bash run_burst_matrix.sh results/my_baseline
python3 report_swss_burst.py results/my_baseline
```

### Apply the tokenize fix

```bash
g++ -O2 -fPIC -std=c++17 -shared -o fast_tokenize.so tokenize_fix.cpp
docker cp fast_tokenize.so swss:/usr/local/lib/fast_tokenize.so

# Patch orchagent.sh inside the container
docker cp swss:/usr/bin/orchagent.sh /tmp/orchagent.sh
sed -i 's|^exec /usr/bin/orchagent|export LD_PRELOAD=/usr/local/lib/fast_tokenize.so\nexec /usr/bin/orchagent|' /tmp/orchagent.sh
docker cp /tmp/orchagent.sh swss:/usr/bin/orchagent.sh

docker exec swss supervisorctl stop orchagent
docker exec -d swss /usr/bin/orchagent.sh
```

### Measure after fix

```bash
COUNT_SET="10000 20000 30000 40000 50000" BATCH_SET="64" DRAIN_TIMEOUT=600 \
  bash run_burst_matrix.sh results/my_fixed
python3 report_swss_burst.py results/my_fixed
```
