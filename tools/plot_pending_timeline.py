#!/usr/bin/env python3
"""Plot APPL_DB pending route-key timelines from SWSS burst results."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

try:
    import matplotlib.pyplot as plt
except ImportError:  # optional plotting dependency
    plt = None


def read_samples(path: Path) -> tuple[list[float], list[int]]:
    times: list[float] = []
    pending: list[int] = []
    first_ts = None
    with path.open(encoding="utf-8") as fp:
        reader = csv.DictReader(fp)
        for row in reader:
            value = row.get("route_pending_keys")
            if value is None or value == "na":
                continue
            try:
                ts = int(row.get("timestamp", "0"))
                route_pending = int(value)
            except ValueError:
                continue
            if first_ts is None:
                first_ts = ts
            times.append(ts - first_ts)
            pending.append(route_pending)
    return times, pending


def plot_pair(run_root: Path, out_dir: Path) -> bool:
    normal = run_root / "normal" / "metrics" / "redis_appldb_samples.csv"
    adaptive = run_root / "adaptive" / "metrics" / "redis_appldb_samples.csv"
    if not normal.exists() and not adaptive.exists():
        return False

    plt.figure(figsize=(10, 5))
    plotted = False
    for label, path in (("normal", normal), ("adaptive", adaptive)):
        if not path.exists():
            continue
        times, pending = read_samples(path)
        if not times:
            continue
        plt.plot(times, pending, label=label)
        plotted = True

    if not plotted:
        plt.close()
        return False

    plt.title(f"Pending route keys: {run_root.name}")
    plt.xlabel("seconds from metrics start")
    plt.ylabel("ROUTE_TABLE_KEY_SET size")
    plt.grid(True, alpha=0.3)
    plt.legend()
    out_dir.mkdir(parents=True, exist_ok=True)
    plt.tight_layout()
    plt.savefig(out_dir / f"{run_root.name}_pending_timeline.png", dpi=140)
    plt.close()
    return True


def plot_single(csv_path: Path, out_dir: Path) -> bool:
    times, pending = read_samples(csv_path)
    if not times:
        return False
    out_dir.mkdir(parents=True, exist_ok=True)
    plt.figure(figsize=(10, 5))
    plt.plot(times, pending, label=str(csv_path.parent.parent))
    plt.title(f"Pending route keys: {csv_path.parent.parent.name}")
    plt.xlabel("seconds from metrics start")
    plt.ylabel("ROUTE_TABLE_KEY_SET size")
    plt.grid(True, alpha=0.3)
    plt.legend()
    plt.tight_layout()
    safe_name = "_".join(csv_path.parent.parent.parts[-2:])
    plt.savefig(out_dir / f"{safe_name}_pending_timeline.png", dpi=140)
    plt.close()
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("results", nargs="?", default="results/adaptive_comparison")
    parser.add_argument("--out-dir", default="charts")
    args = parser.parse_args()

    if plt is None:
        parser.error("matplotlib is required for plotting; install python3-matplotlib")

    root = Path(args.results)
    out_dir = Path(args.out_dir)
    if not root.exists():
        parser.error(f"{root} does not exist")

    count = 0
    pairs = [p.parents[2] for p in root.rglob("normal/metrics/redis_appldb_samples.csv")]
    if pairs:
        for run_root in sorted(pairs):
            if plot_pair(run_root, out_dir):
                count += 1
    else:
        for csv_path in sorted(root.rglob("redis_appldb_samples.csv")):
            if plot_single(csv_path, out_dir):
                count += 1

    print(f"wrote {count} chart(s) to {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
