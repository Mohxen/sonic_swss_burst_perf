#!/usr/bin/env python3
"""Summarize SWSS burst injector and metric output."""

from __future__ import annotations

import csv
import json
import re
import sys
from pathlib import Path


def read_jsonl(path: Path) -> list[dict]:
    events = []
    with path.open(encoding="utf-8") as fp:
        for line in fp:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events


def summarize_injectors(root: Path) -> None:
    files = sorted(root.rglob("*.jsonl"))
    if not files:
        print("No injector JSONL files found.")
        return
    print("Injector summaries:")
    for path in files:
        events = read_jsonl(path)
        start = next((e for e in events if e.get("event") == "start"), {})
        end = next((e for e in reversed(events) if e.get("event") == "end"), {})
        if not end:
            print(f"- {path}: incomplete")
            continue
        rps = end.get("routes_per_second")
        p50 = end.get("batch_seconds_p50")
        p95 = end.get("batch_seconds_p95")
        print(
            f"- {path}: op={start.get('op')} count={end.get('count')} "
            f"batch={start.get('batch_size')} total={end.get('total_seconds'):.3f}s "
            f"rate={rps:.1f}/s p50_batch={p50:.6f}s p95_batch={p95:.6f}s"
        )


def summarize_pending(root: Path) -> None:
    files = sorted(root.rglob("redis_appldb_samples.csv"))
    if not files:
        print("No Redis sample CSV files found.")
        return
    print("\nAPPL_DB pending-key samples:")
    for path in files:
        max_pending = None
        max_ts = None
        last_pending = None
        last_ts = None
        rows = 0
        with path.open(encoding="utf-8") as fp:
            reader = csv.DictReader(fp)
            for row in reader:
                rows += 1
                value = row.get("route_pending_keys")
                if value is None or value == "na":
                    continue
                try:
                    pending = int(value)
                    timestamp = int(row.get("timestamp", "0"))
                except ValueError:
                    continue
                last_pending = pending
                last_ts = timestamp
                if max_pending is None or pending > max_pending:
                    max_pending = pending
                    max_ts = timestamp
        drain_rate = None
        if (
            max_pending is not None
            and last_pending is not None
            and max_ts is not None
            and last_ts is not None
            and last_ts > max_ts
            and max_pending >= last_pending
        ):
            drain_rate = (max_pending - last_pending) / (last_ts - max_ts)
        drain_text = "na" if drain_rate is None else f"{drain_rate:.1f}/s_after_peak"
        status = "drained" if last_pending == 0 else "not_drained"
        print(
            f"- {path}: samples={rows} max_pending={max_pending} "
            f"last_pending={last_pending} {status} drain_rate={drain_text}"
        )


def summarize_drain_waits(root: Path) -> None:
    files = sorted(root.rglob("drain_wait_*.csv"))
    if not files:
        return
    print("\nExplicit drain waits:")
    for path in files:
        rows = 0
        last_pending = None
        last_elapsed = None
        with path.open(encoding="utf-8") as fp:
            reader = csv.DictReader(fp)
            for row in reader:
                rows += 1
                value = row.get("route_pending_keys")
                if value is None or value == "na":
                    continue
                try:
                    last_pending = int(value)
                    last_elapsed = int(row.get("elapsed_seconds", "0"))
                except ValueError:
                    continue
        status = "drained" if last_pending == 0 else "timeout_or_not_drained"
        print(
            f"- {path}: samples={rows} elapsed={last_elapsed}s "
            f"last_pending={last_pending} {status}"
        )


def summarize_diagnostics(root: Path) -> None:
    files = sorted(root.rglob("redis_diagnostics.log"))
    if not files:
        print("No Redis diagnostic logs found.")
        return
    print("\nRedis diagnostics:")
    slowlog_re = re.compile(r"slowlog|get|hset|publish|sadd|lpush|eval", re.I)
    latency_re = re.compile(r"latency|commandstats|cmdstat_", re.I)
    for path in files:
        slow_hits = 0
        latency_lines = 0
        with path.open(encoding="utf-8", errors="replace") as fp:
            for line in fp:
                if slowlog_re.search(line):
                    slow_hits += 1
                if latency_re.search(line):
                    latency_lines += 1
        print(
            f"- {path}: slowlog_related_lines={slow_hits} "
            f"latency_or_commandstat_lines={latency_lines}"
        )


def main(argv: list[str]) -> int:
    root = Path(argv[0]) if argv else Path("results")
    if not root.exists():
        print(f"{root} does not exist", file=sys.stderr)
        return 2
    summarize_injectors(root)
    summarize_pending(root)
    summarize_drain_waits(root)
    summarize_diagnostics(root)
    print("\nInterpretation:")
    print("- High pending keys after injection stops points at orchagent/event processing.")
    print("- Redis slowlog or latency spikes with low orchagent CPU points at Redis.")
    print("- syncd saturation after ASIC_DB updates points at virtual SAI/syncd.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
