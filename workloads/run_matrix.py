#!/usr/bin/env python3
import sys
import os
import subprocess
import argparse
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

def run_matrix(split: str, dry_run: bool = False, max_runs: int = 0):
    scratch_dir = Path.home() / "ebpf_lab" / "scratch"
    if not dry_run:
        try:
            scratch_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
    test_bin = scratch_dir / "test.bin"
    out_base = REPO_ROOT / "data" / split
    if not dry_run:
        try:
            out_base.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

    # Core factorial combinations for the split
    configs = [
        # (run_name, mode, param, bs, iters)
        (f"{split}_cpu_50ms_c1", "cpu", "50", "1048576", 5),
        (f"{split}_cpu_20ms_c1", "cpu", "20", "1048576", 5),
        (f"{split}_io_1mb_cold_c1", "io", str(test_bin), "1048576", 5),
        (f"{split}_io_64kb_cold_c1", "io", str(test_bin), "65536", 5),
        (f"{split}_warm_1mb_c1", "warm", str(test_bin), "1048576", 5),
        (f"{split}_warm_64kb_c1", "warm", str(test_bin), "65536", 5),
        (f"{split}_sched_4comp_c1", "sched", "4", "1048576", 5),
        (f"{split}_sched_8comp_c1", "sched", "8", "1048576", 5),
        (f"{split}_mixed_io_sched_c1", "mixed", str(test_bin), "1048576", 3),
    ]

    if max_runs > 0:
        configs = configs[:max_runs]

    print(f"Matrix runner: {len(configs)} scenario runs configured for target '{split}'.")

    run_script = REPO_ROOT / "workloads" / "run_scenario.sh"

    for run_name, mode, param, bs, iters in configs:
        run_dir = out_base / run_name
        cmd = [str(run_script), str(run_dir), mode, param, str(bs), str(iters)]
        cmd_str = " ".join(cmd)

        if dry_run:
            print(f"[dry-run] sudo {cmd_str}")
            continue

        print(f"\n---> Executing scenario: {run_name} (mode: {mode}, iters: {iters})")
        res = subprocess.run(["sudo"] + cmd)
        if res.returncode != 0:
            print(f"Error: command failed with code {res.returncode}: {cmd_str}")

def main():
    parser = argparse.ArgumentParser(description="Factorial scenarios runner")
    parser.add_argument("--split", choices=["tune", "final"], default="tune", help="Target data split directory")
    parser.add_argument("--dry-run", action="store_true", help="Print execution commands without running")
    parser.add_argument("--max-runs", type=int, default=0, help="Cap maximum number of runs executed")
    args = parser.parse_args()

    run_matrix(args.split, args.dry_run, args.max_runs)

if __name__ == "__main__":
    main()
