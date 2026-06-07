#!/usr/bin/env python3
"""Summarize SWSS burst injector and metric output."""

from __future__ import annotations

import csv
import json
import re
import sys
from pathlib import Path


def fmt_float(value, digits: int = 3) -> str:
    if value is None:
        return "na"
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return "na"


def read_jsonl(path: Path) -> list[dict]:
    events = []
    with path.open(encoding="utf-8") as fp:
        for line in fp:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events


def read_inject_summary(path: Path) -> dict:
    if not path.exists():
        return {"start": {}, "end": {}, "batches": []}
    events = read_jsonl(path)
    start = next((e for e in events if e.get("event") == "start"), {})
    end = next((e for e in reversed(events) if e.get("event") == "end"), {})
    batches = [e for e in events if e.get("event") == "batch"]
    return {"start": start, "end": end, "batches": batches}


def summarize_injectors(root: Path) -> None:
    files = sorted(root.rglob("*.jsonl"))
    if not files:
        print("No injector JSONL files found.")
        return
    print("Injector summaries:")
    for path in files:
        summary = read_inject_summary(path)
        start = summary["start"]
        end = summary["end"]
        if not end:
            print(f"- {path}: incomplete")
            continue
        adaptive = ""
        if "max_pending_seen" in end or "final_batch_size" in end:
            adaptive = (
                f" max_pending_seen={end.get('max_pending_seen')}"
                f" avg_pending_seen={fmt_float(end.get('average_pending_seen'), 1)}"
                f" final_batch={end.get('final_batch_size')}"
                f" final_sleep_ms={end.get('final_sleep_ms')}"
            )
        print(
            f"- {path}: op={start.get('op')} count={end.get('count')} "
            f"batch={start.get('batch_size')} total={fmt_float(end.get('total_seconds'))}s "
            f"rate={fmt_float(end.get('routes_per_second'), 1)}/s "
            f"p50_batch={fmt_float(end.get('batch_seconds_p50'), 6)}s "
            f"p95_batch={fmt_float(end.get('batch_seconds_p95'), 6)}s{adaptive}"
        )


def redis_pending_summary(path: Path) -> dict:
    summary = {"max_pending": None, "last_pending": None, "rows": 0}
    if not path.exists():
        return summary
    with path.open(encoding="utf-8") as fp:
        reader = csv.DictReader(fp)
        for row in reader:
            summary["rows"] += 1
            value = row.get("route_pending_keys")
            if value is None or value == "na":
                continue
            try:
                pending = int(value)
            except ValueError:
                continue
            summary["last_pending"] = pending
            if summary["max_pending"] is None or pending > summary["max_pending"]:
                summary["max_pending"] = pending
    return summary


def drain_wait_elapsed(path: Path):
    if not path.exists():
        return None
    last_elapsed = None
    with path.open(encoding="utf-8") as fp:
        reader = csv.DictReader(fp)
        for row in reader:
            try:
                last_elapsed = int(row.get("elapsed_seconds", "0"))
            except ValueError:
                continue
    return last_elapsed


def comparison_row(run_dir: Path) -> dict:
    add = read_inject_summary(run_dir / "inject_add.jsonl")
    delete = read_inject_summary(run_dir / "inject_del.jsonl")
    add_end = add.get("end", {})
    pending = redis_pending_summary(run_dir / "metrics" / "redis_appldb_samples.csv")
    max_pending = add_end.get("max_pending_seen")
    if max_pending is None:
        max_pending = pending.get("max_pending")
    return {
        "enqueue_s": add_end.get("total_seconds"),
        "routes_per_second": add_end.get("routes_per_second"),
        "max_pending": max_pending,
        "add_drain_s": drain_wait_elapsed(run_dir / "drain_wait_add.csv"),
        "del_drain_s": drain_wait_elapsed(run_dir / "drain_wait_del.csv"),
        "final_pending": pending.get("last_pending"),
        "average_pending": add_end.get("average_pending_seen"),
        "delete_s": delete.get("end", {}).get("total_seconds"),
    }


def summarize_adaptive_comparisons(root: Path) -> None:
    pairs = []
    for normal_dir in sorted(root.rglob("normal")):
        adaptive_dir = normal_dir.parent / "adaptive"
        if (
            normal_dir.is_dir()
            and adaptive_dir.is_dir()
            and (normal_dir / "inject_add.jsonl").exists()
            and (adaptive_dir / "inject_add.jsonl").exists()
        ):
            pairs.append((normal_dir.parent, normal_dir, adaptive_dir))
    if not pairs:
        return

    print("\nNormal vs adaptive comparison:")
    print(
        "| run | mode | enqueue_s | routes/s | max_pending | add_drain_s | "
        "del_drain_s | final_pending | avg_pending |"
    )
    print("| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for run_root, normal_dir, adaptive_dir in pairs:
        for mode, run_dir in (("normal", normal_dir), ("adaptive", adaptive_dir)):
            row = comparison_row(run_dir)
            print(
                f"| {run_root.name} | {mode} | "
                f"{fmt_float(row['enqueue_s'])} | "
                f"{fmt_float(row['routes_per_second'], 1)} | "
                f"{row['max_pending'] if row['max_pending'] is not None else 'na'} | "
                f"{row['add_drain_s'] if row['add_drain_s'] is not None else 'na'} | "
                f"{row['del_drain_s'] if row['del_drain_s'] is not None else 'na'} | "
                f"{row['final_pending'] if row['final_pending'] is not None else 'na'} | "
                f"{fmt_float(row['average_pending'], 1)} |"
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
    summarize_adaptive_comparisons(root)
    summarize_diagnostics(root)
    print("\nInterpretation:")
    print("- High pending keys after injection stops points at orchagent/event processing.")
    print("- Redis slowlog or latency spikes with low orchagent CPU points at Redis.")
    print("- syncd saturation after ASIC_DB updates points at virtual SAI/syncd.")
    print("- Adaptive runs trade producer speed for lower pending depth and shorter long-tail drain.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
