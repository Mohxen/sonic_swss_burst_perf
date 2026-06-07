#!/usr/bin/env python3
"""Backpressure-aware route burst injector for SONiC APPL_DB ROUTE_TABLE."""

from __future__ import annotations

import argparse
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from route_burst import (
    EventLogger,
    build_writer,
    percentile,
    route_prefixes,
)


class PendingReader:
    def __init__(self, key: str = "ROUTE_TABLE_KEY_SET"):
        self.key = key
        self.commands = [
            ["sonic-db-cli", "APPL_DB", "SCARD", key],
            ["redis-cli", "-n", "0", "SCARD", key],
            ["docker", "exec", "database", "redis-cli", "-n", "0", "SCARD", key],
        ]
        self.selected: Optional[list[str]] = None
        self.warned = False

    def read(self) -> Optional[int]:
        commands = [self.selected] if self.selected else self.commands
        for command in commands:
            if command is None:
                continue
            try:
                result = subprocess.run(
                    command,
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                value = int(result.stdout.strip().splitlines()[-1])
                self.selected = command
                return value
            except (FileNotFoundError, subprocess.SubprocessError, ValueError, IndexError):
                if self.selected is not None:
                    self.selected = None
                    return None
                continue
        if not self.warned:
            print("warning: unable to read ROUTE_TABLE_KEY_SET pending count", file=sys.stderr)
            self.warned = True
        return None


def clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def adjust_control(args, pending: Optional[int], batch_size: int, sleep_ms: int) -> tuple[int, int]:
    if pending is None:
        return batch_size, min(args.sleep_ms_max, max(sleep_ms, args.sleep_ms_min))

    if pending < args.low_pending:
        next_batch = min(args.max_batch_size, max(batch_size + args.min_batch_size, int(batch_size * 1.25)))
        next_sleep = max(args.sleep_ms_min, sleep_ms // 2)
    elif pending <= args.target_pending:
        next_batch = batch_size
        next_sleep = sleep_ms
    elif pending < args.high_pending:
        next_batch = max(args.min_batch_size, int(batch_size * 0.75))
        next_sleep = min(args.sleep_ms_max, max(sleep_ms + 10, args.sleep_ms_min))
    else:
        next_batch = args.min_batch_size
        next_sleep = max(args.sleep_ms_min, int(args.sleep_ms_max * 0.8))

    return (
        clamp(next_batch, args.min_batch_size, args.max_batch_size),
        clamp(next_sleep, args.sleep_ms_min, args.sleep_ms_max),
    )


def run(args) -> int:
    writer = build_writer(args)
    jsonl = Path(args.jsonl) if args.jsonl else None
    pending_reader = PendingReader()

    prefixes = list(route_prefixes(args.prefix_base, args.prefix_len, args.count))
    if args.reverse:
        prefixes.reverse()

    selected_batch_size = clamp(args.batch_size, args.min_batch_size, args.max_batch_size)
    selected_sleep_ms = clamp(args.sleep_ms_min, args.sleep_ms_min, args.sleep_ms_max)
    pending_samples: list[int] = []
    batch_latencies: list[float] = []

    with EventLogger(jsonl) as logger:
        logger.emit(
            {
                "event": "start",
                "time": time.time(),
                "op": args.op,
                "count": args.count,
                "batch_size": args.batch_size,
                "selected_batch_size": selected_batch_size,
                "selected_sleep_ms": selected_sleep_ms,
                "table": args.table,
                "prefix_base": args.prefix_base,
                "prefix_len": args.prefix_len,
                "nexthop": args.nexthop,
                "ifname": args.ifname,
                "target_pending": args.target_pending,
                "high_pending": args.high_pending,
                "low_pending": args.low_pending,
            },
        )

        total_start = time.perf_counter()
        done = 0
        batches_since_check = args.pending_check_interval
        pending = pending_reader.read()
        if pending is not None:
            pending_samples.append(pending)

        while done < len(prefixes):
            batch = prefixes[done : done + selected_batch_size]
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

            batches_since_check += 1
            if batches_since_check >= args.pending_check_interval or done == len(prefixes):
                pending = pending_reader.read()
                batches_since_check = 0
                if pending is not None:
                    pending_samples.append(pending)

            logger.emit(
                {
                    "event": "batch",
                    "time": time.time(),
                    "op": args.op,
                    "done": done,
                    "batch_count": len(batch),
                    "batch_seconds": elapsed,
                    "routes_per_second": len(batch) / elapsed if elapsed else None,
                    "pending_keys": pending,
                    "selected_batch_size": selected_batch_size,
                    "selected_sleep_ms": selected_sleep_ms,
                },
            )

            selected_batch_size, selected_sleep_ms = adjust_control(
                args, pending, selected_batch_size, selected_sleep_ms
            )
            if selected_sleep_ms:
                time.sleep(selected_sleep_ms / 1000.0)

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
                "max_pending_seen": max(pending_samples) if pending_samples else None,
                "average_pending_seen": statistics.fmean(pending_samples) if pending_samples else None,
                "final_batch_size": selected_batch_size,
                "final_sleep_ms": selected_sleep_ms,
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
    parser.add_argument("--reverse", action="store_true")
    parser.add_argument("--jsonl")
    parser.add_argument("--target-pending", type=int, default=10000)
    parser.add_argument("--high-pending", type=int, default=20000)
    parser.add_argument("--low-pending", type=int, default=5000)
    parser.add_argument("--min-batch-size", type=int, default=32)
    parser.add_argument("--max-batch-size", type=int, default=512)
    parser.add_argument("--sleep-ms-min", type=int, default=0)
    parser.add_argument("--sleep-ms-max", type=int, default=500)
    parser.add_argument("--pending-check-interval", type=int, default=1)
    args = parser.parse_args(argv)

    if args.count <= 0:
        parser.error("--count must be positive")
    if args.batch_size <= 0:
        parser.error("--batch-size must be positive")
    if args.min_batch_size <= 0:
        parser.error("--min-batch-size must be positive")
    if args.max_batch_size < args.min_batch_size:
        parser.error("--max-batch-size must be >= --min-batch-size")
    if not (0 <= args.low_pending <= args.target_pending <= args.high_pending):
        parser.error("--low-pending <= --target-pending <= --high-pending is required")
    if args.sleep_ms_min < 0 or args.sleep_ms_max < args.sleep_ms_min:
        parser.error("--sleep-ms-max must be >= --sleep-ms-min >= 0")
    if args.pending_check_interval <= 0:
        parser.error("--pending-check-interval must be positive")
    return args


if __name__ == "__main__":
    raise SystemExit(run(parse_args(sys.argv[1:])))
