#!/usr/bin/env python3
import json
import sys
import csv
from pathlib import Path

try:
    import pandas as pd
except ImportError:
    pd = None

def validate_run(run_dir):
    run_path = Path(run_dir)
    print(f"\nValidating run directory: {run_path.name}")

    # 1. Check file existence
    truth_file = run_path / "truth.jsonl"
    events_file = run_path / "events.csv"
    drops_file = run_path / "drops.txt"

    if not all([truth_file.exists(), events_file.exists(), drops_file.exists()]):
        print("FAILED: Missing one or more required output files.")
        return False

    # 2. Check dropped events
    drops = int(drops_file.read_text().strip())
    print(f"Dropped events: {drops}")
    if drops > 0:
        print(f"WARNING: Ring buffer dropped {drops} events during this run.")

    # 3. Read Ground Truth
    with open(truth_file) as f:
        truth_lines = [json.loads(line) for line in f if line.strip()]

    if not truth_lines:
        print("FAILED: truth.jsonl is empty.")
        return False

    truth = truth_lines[0]
    tid = truth["tid"]
    t_start = truth["start_ns"]
    t_end = truth["end_ns"]
    label = truth["label"]
    duration_ms = (t_end - t_start) / 1e6

    print(f"Ground truth op: TID={tid}, Label={label}, Duration={duration_ms:.2f} ms")

    # 4. Load Events and Slice Time Window
    if pd is not None:
        df = pd.read_csv(events_file)
        if df.empty:
            print("FAILED: events.csv is empty.")
            return False
        window_events = df[(df.ts_ns >= t_start) & (df.ts_ns <= t_end)]
        thread_events = window_events[window_events.tid == tid]
        n_window = len(window_events)
        n_thread = len(thread_events)
        off_cpu_sleeps_count = len(thread_events[(thread_events.layer == 2) & (thread_events.type == 0) & (thread_events.prev_state == 2)])
        block_issues = len(window_events[(window_events.layer == 3) & (window_events.type == 0)])
        block_completes = len(window_events[(window_events.layer == 3) & (window_events.type == 1)])
        block_events_count = len(window_events[window_events.layer == 3])
        d_state_sleeps_count = off_cpu_sleeps_count
        sched_switches_count = len(thread_events[thread_events.layer == 2])
    else:
        with open(events_file, mode="r", newline="") as f:
            reader = csv.DictReader(f)
            all_rows = list(reader)
        if not all_rows:
            print("FAILED: events.csv is empty.")
            return False
        window_rows = [r for r in all_rows if t_start <= int(r["ts_ns"]) <= t_end]
        thread_rows = [r for r in window_rows if int(r["tid"]) == tid]
        n_window = len(window_rows)
        n_thread = len(thread_rows)
        off_cpu_sleeps_count = sum(1 for r in thread_rows if int(r["layer"]) == 2 and int(r["type"]) == 0 and int(r["prev_state"]) == 2)
        block_issues = sum(1 for r in window_rows if int(r["layer"]) == 3 and int(r["type"]) == 0)
        block_completes = sum(1 for r in window_rows if int(r["layer"]) == 3 and int(r["type"]) == 1)
        block_events_count = sum(1 for r in window_rows if int(r["layer"]) == 3)
        d_state_sleeps_count = off_cpu_sleeps_count
        sched_switches_count = sum(1 for r in thread_rows if int(r["layer"]) == 2)

    print(f"Events captured in window: {n_window} total, {n_thread} on target TID")

    # 5. Diagnostic Assertion by Expected Label
    passed = True
    if drops > 0:
        passed = False

    if label in ("CPU_SYSCALL", "CPU_STALL"):
        # Target thread should stay on-CPU without going into D-state sleep
        if off_cpu_sleeps_count == 0:
            print("PASSED: No uninterruptible storage sleeps detected during CPU spin.")
        else:
            print(f"UNEXPECTED: Found {off_cpu_sleeps_count} D-state sleep events during pure CPU spin.")
            passed = False

    elif label == "IO_WAIT":
        # Must observe Layer 3 block issue/complete events
        print(f"    - Block I/O issues (layer 3, type 0): {block_issues}")
        print(f"    - Block I/O completes (layer 3, type 1): {block_completes}")
        print(f"    - Thread D-state sleeps (prev_state=2): {d_state_sleeps_count}")

        if block_issues > 0 and block_completes > 0 and d_state_sleeps_count > 0:
            if abs(block_issues - block_completes) <= 2:
                print("PASSED: Block issue/complete counts balanced and uninterruptible sleep captured.")
            else:
                print(f"WARNING: Imbalance between block issues ({block_issues}) and completes ({block_completes}).")
        else:
            print("FAILED: Missing block activity or D-state sleep for cold I/O wait.")
            passed = False

    elif label == "WARM_READ":
        # Buffered reads hitting RAM should produce minimal or 0 block requests
        print(f"    - Block I/O events during warm read: {block_events_count}")
        if block_events_count == 0:
            print("PASSED: Zero physical block requests observed (Page Cache hit verified).")
        else:
            print("NOTE: Background block events present, verify if they belong to target file.")

    elif label == "SCHED_WAIT":
        # Thread should experience sched_switch / sched_wakeup transitions with runnable delays
        print(f"    - Sched events on target thread: {sched_switches_count}")
        if sched_switches_count > 0:
            print("PASSED: Scheduler context switch transitions observed.")
        else:
            print("FAILED: Target thread showed no scheduler interaction.")
            passed = False

    return passed

if __name__ == "__main__":
    test_dirs = [
        "data/tune/test_cpu",
        "data/tune/test_io",
        "data/tune/test_warm",
        "data/tune/test_sched"
    ]
    
    results = {}
    for d in test_dirs:
        if Path(d).exists():
            results[d] = validate_run(d)
        else:
            print(f"Skipping {d} (directory not found)")

    print("\nSummary:")
    for d, success in results.items():
        status = "PASSED" if success else "FAILED"
        print(f"{d}: {status}")