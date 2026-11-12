#!/usr/bin/env python3
import sys
import json
import glob
from pathlib import Path

try:
    import pandas as pd
except ImportError:
    pd = None

import csv

def load_thresholds():
    thresh_file = Path(__file__).resolve().parent / "thresholds.json"
    defaults = {
        "io_sleep_ratio_min": 0.45,
        "sched_wait_ratio_min": 0.45,
        "on_cpu_ratio_min": 0.75,
        "warm_read_dur_max_ms": 5.0,
        "abstain_phantom_io_sleep_min": 0.40,
        "abstain_bg_noise_on_cpu_min": 0.80,
        "abstain_mixed_ratio_min": 0.30
    }
    if thresh_file.exists():
        try:
            with open(thresh_file, "r") as f:
                data = json.load(f)
                defaults.update(data)
        except Exception:
            pass
    return defaults

THRESHOLDS = load_thresholds()

def classify_row(row: dict) -> tuple[str, float]:
    duration_ms = float(row.get("duration_ms", 0.0))
    on_cpu_ratio = float(row.get("on_cpu_ratio", 0.0))
    sched_wait_ratio = float(row.get("sched_wait_ratio", 0.0))
    io_sleep_ratio = float(row.get("io_sleep_ratio", 0.0))
    block_issues = int(row.get("block_issues", 0))
    sys_enters = int(row.get("sys_enters", 0))

    # Abstention triggers
    if io_sleep_ratio >= THRESHOLDS["abstain_phantom_io_sleep_min"] and block_issues == 0:
        return "INDETERMINATE", 0.10

    if block_issues > 0 and on_cpu_ratio >= THRESHOLDS["abstain_bg_noise_on_cpu_min"]:
        return "INDETERMINATE", 0.20

    if io_sleep_ratio >= THRESHOLDS["abstain_mixed_ratio_min"] and sched_wait_ratio >= THRESHOLDS["abstain_mixed_ratio_min"]:
        return "INDETERMINATE", 0.30

    # Decision rules
    if io_sleep_ratio >= THRESHOLDS["io_sleep_ratio_min"] and block_issues > 0:
        conf = min(io_sleep_ratio + 0.10, 1.0)
        return "IO_WAIT", round(conf, 3)

    if sched_wait_ratio >= THRESHOLDS["sched_wait_ratio_min"]:
        conf = min(sched_wait_ratio + 0.10, 1.0)
        return "SCHED_WAIT", round(conf, 3)

    if duration_ms <= THRESHOLDS["warm_read_dur_max_ms"] and block_issues == 0 and sys_enters > 0:
        return "WARM_READ", 0.900

    if on_cpu_ratio >= THRESHOLDS["on_cpu_ratio_min"] and block_issues == 0:
        conf = min(on_cpu_ratio, 1.0)
        return "CPU_SYSCALL", round(conf, 3)

    return "INDETERMINATE", 0.000

def run_rules_on_dir(run_dir: str):
    run_path = Path(run_dir)
    feat_parquet = run_path / "features.parquet"
    feat_csv = run_path / "features.csv"
    out_file = run_path / "pred_rules.jsonl"

    rows = []
    if feat_parquet.exists() and pd is not None:
        try:
            df = pd.read_parquet(feat_parquet)
            rows = df.to_dict(orient="records")
        except Exception:
            pass

    if not rows and feat_csv.exists():
        with open(feat_csv, "r", newline="") as f:
            reader = csv.DictReader(f)
            rows = list(reader)

    if not rows:
        print(f"Skipping {run_dir}: neither features.parquet nor features.csv found")
        return

    predictions = []
    for row in rows:
        pred_label, conf = classify_row(row)
        if pred_label == "IO_WAIT":
            est_stall_ns = int(row.get("t_blocked", 0))
        elif pred_label == "SCHED_WAIT":
            est_stall_ns = int(row.get("t_runnable", 0))
        else:
            est_stall_ns = 0

        rec = {
            "run_id": str(row.get("run_id", run_path.name)),
            "op_id": int(row.get("op_id", 0)),
            "pred": pred_label,
            "conf": conf,
            "method": "heuristic_rules",
            "est_stall_ns": est_stall_ns,
            "pred_label": pred_label,
            "confidence": conf,
            "tid": int(row.get("tid", 0)),
            "true_label": str(row.get("true_label", row.get("label", "UNKNOWN")))
        }
        predictions.append(rec)

    try:
        with open(out_file, "w") as f:
            for p in predictions:
                f.write(json.dumps(p) + "\n")
        print(f"[rules] Wrote {len(predictions)} predictions to {out_file}")
    except PermissionError:
        print(f"[rules] error: permission denied writing {out_file}.")
        print(f"   Fix ownership with: sudo chown -R $USER:$USER {run_dir}")

def main():
    if len(sys.argv) < 2:
        print("Usage: python3 analysis/rules.py <run_dir_or_glob> [more_dirs...]")
        sys.exit(1)

    targets = []
    for arg in sys.argv[1:]:
        matches = glob.glob(arg)
        if matches:
            targets.extend(matches)
        else:
            targets.append(arg)

    for target in targets:
        run_rules_on_dir(target)

if __name__ == "__main__":
    main()
