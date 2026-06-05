#!/usr/bin/env python3
"""Inject BGP-like route bursts into SONiC APPL_DB ROUTE_TABLE.

The representative mode uses swsscommon.ProducerStateTable so orchagent receives
normal table notifications. A Redis fallback exists for smoke tests only.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterable, Optional


def load_swsscommon():
    try:
        from swsscommon import swsscommon  # type: ignore

        return swsscommon
    except Exception:
        try:
            import swsscommon  # type: ignore

            return swsscommon
        except Exception:
            return None


def route_prefixes(base: str, prefix_len: int, count: int) -> Iterable[str]:
    network = ipaddress.ip_network(f"{base}/{prefix_len}", strict=False)
    start = int(network.network_address)
    step = 1 if prefix_len in (32, 128) else 1 << (network.max_prefixlen - prefix_len)
    max_addr = (1 << network.max_prefixlen) - 1
    for i in range(count):
        addr_int = start + (i * step)
        if addr_int > max_addr:
            raise ValueError("prefix generation exceeded address family range")
        yield f"{ipaddress.ip_address(addr_int)}/{prefix_len}"


class EventLogger:
    def __init__(self, path: Optional[Path]):
        self.path = path
        self.fp = None
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.fp = path.open("w", encoding="utf-8")

    def emit(self, event: dict) -> None:
        line = json.dumps(event, sort_keys=True)
        if self.fp is None:
            print(line, flush=True)
            return
        self.fp.write(line + "\n")
        self.fp.flush()

    def close(self) -> None:
        if self.fp is not None:
            self.fp.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()


class ProducerRouteWriter:
    def __init__(self, table: str):
        swsscommon = load_swsscommon()
        if swsscommon is None:
            raise RuntimeError("swsscommon is unavailable")
        self.swsscommon = swsscommon
        self.db = swsscommon.DBConnector("APPL_DB", 0, True)
        self.table = swsscommon.ProducerStateTable(self.db, table)

    def set_route(self, prefix: str, nexthop: str, ifname: str) -> None:
        fvs = self.swsscommon.FieldValuePairs(
            [("nexthop", nexthop), ("ifname", ifname)]
        )
        self.table.set(prefix, fvs)

    def del_route(self, prefix: str) -> None:
        if hasattr(self.table, "_del"):
            self.table._del(prefix)
        else:
            self.table.delete(prefix)


class RedisCliRouteWriter:
    def __init__(self, table: str, redis_cli: str):
        self.table = table
        self.redis_cli = redis_cli

    def _run(self, args: list[str]) -> None:
        subprocess.run([self.redis_cli, "-n", "0", *args], check=True)

    def set_route(self, prefix: str, nexthop: str, ifname: str) -> None:
        key = f"{self.table}:{prefix}"
        self._run(["HSET", key, "nexthop", nexthop, "ifname", ifname])

    def del_route(self, prefix: str) -> None:
        key = f"{self.table}:{prefix}"
        self._run(["DEL", key])


def percentile(values: list[float], pct: float) -> Optional[float]:
    if not values:
        return None
    values = sorted(values)
    index = int(round((len(values) - 1) * pct))
    return values[index]


def build_writer(args):
    if args.mode == "producer":
        return ProducerRouteWriter(args.table)
    if args.mode == "redis-cli":
        return RedisCliRouteWriter(args.table, args.redis_cli)
    if load_swsscommon() is not None:
        return ProducerRouteWriter(args.table)
    print(
        "warning: swsscommon unavailable; using redis-cli smoke-test mode only",
        file=sys.stderr,
    )
    return RedisCliRouteWriter(args.table, args.redis_cli)


def run(args) -> int:
    writer = build_writer(args)
    jsonl = Path(args.jsonl) if args.jsonl else None

    prefixes = list(route_prefixes(args.prefix_base, args.prefix_len, args.count))
    if args.reverse:
        prefixes.reverse()

    with EventLogger(jsonl) as logger:
        logger.emit(
            {
                "event": "start",
                "time": time.time(),
                "op": args.op,
                "count": args.count,
                "batch_size": args.batch_size,
                "table": args.table,
                "prefix_base": args.prefix_base,
                "prefix_len": args.prefix_len,
                "nexthop": args.nexthop,
                "ifname": args.ifname,
            },
        )

        batch_latencies = []
        total_start = time.perf_counter()
        done = 0

        for batch_start in range(0, len(prefixes), args.batch_size):
            batch = prefixes[batch_start : batch_start + args.batch_size]
            start = time.perf_counter()
            for prefix in batch:
                if args.op in ("add", "replace"):
                    writer.set_route(prefix, args.nexthop, args.ifname)
                elif args.op == "del":
                    writer.del_route(prefix)
                else:
                    raise ValueError(f"unsupported op: {args.op}")
            elapsed = time.perf_counter() - start
            batch_latencies.append(elapsed)
            done += len(batch)
            logger.emit(
                {
                    "event": "batch",
                    "time": time.time(),
                    "op": args.op,
                    "done": done,
                    "batch_count": len(batch),
                    "batch_seconds": elapsed,
                    "routes_per_second": len(batch) / elapsed if elapsed else None,
                },
            )
            if args.sleep_ms:
                time.sleep(args.sleep_ms / 1000.0)

        total_elapsed = time.perf_counter() - total_start
        logger.emit(
            {
                "event": "end",
                "time": time.time(),
                "op": args.op,
                "count": args.count,
                "total_seconds": total_elapsed,
                "routes_per_second": args.count / total_elapsed if total_elapsed else None,
                "batch_seconds_p50": percentile(batch_latencies, 0.50),
                "batch_seconds_p95": percentile(batch_latencies, 0.95),
                "batch_seconds_max": max(batch_latencies) if batch_latencies else None,
            },
        )
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, required=True)
    parser.add_argument("--batch-size", type=int, default=256)
    parser.add_argument("--op", choices=["add", "replace", "del"], default="add")
    parser.add_argument("--prefix-base", default="100.64.0.0")
    parser.add_argument("--prefix-len", type=int, default=32)
    parser.add_argument("--nexthop", default="10.0.0.1")
    parser.add_argument("--ifname", default="Ethernet0")
    parser.add_argument("--table", default="ROUTE_TABLE")
    parser.add_argument("--mode", choices=["auto", "producer", "redis-cli"], default="auto")
    parser.add_argument("--redis-cli", default="redis-cli")
    parser.add_argument("--sleep-ms", type=int, default=0)
    parser.add_argument("--reverse", action="store_true")
    parser.add_argument("--jsonl")
    args = parser.parse_args(argv)
    if args.count <= 0:
        parser.error("--count must be positive")
    if args.batch_size <= 0:
        parser.error("--batch-size must be positive")
    return args


if __name__ == "__main__":
    raise SystemExit(run(parse_args(sys.argv[1:])))
