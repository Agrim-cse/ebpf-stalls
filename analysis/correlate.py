#!/usr/bin/env python3
import sys
import json
import glob
from pathlib import Path

try:
    import pandas as pd
    import numpy as np
except ImportError:
    pd = None
    np = None

import csv

def validate_label_signature(label: str, dur_ms: float, on_cpu_r: float, sched_r: float, io_sleep_r: float, blk_issues: int, n_ctx: int) -> tuple[bool, str]:
    if label == "CPU_SYSCALL":
        if blk_issues > 0:
            return False, f"CPU_SYSCALL had {blk_issues} block requests"
        if io_sleep_r > 0.10:
            return False, f"CPU_SYSCALL had D-state sleep ratio {io_sleep_r:.2f}"
        if on_cpu_r < 0.50:
            return False, f"CPU_SYSCALL had low on-CPU ratio {on_cpu_r:.2f}"
        return True, ""
    elif label == "IO_WAIT":
        if blk_issues == 0:
            return False, "IO_WAIT had zero block issues"
        if io_sleep_r == 0:
            return False, "IO_WAIT had zero D-state sleep"
        return True, ""
    elif label == "WARM_READ":
        if blk_issues > 0:
            return False, f"WARM_READ had {blk_issues} block issues (cache miss)"
        if dur_ms > 5.0:
            return False, f"WARM_READ duration {dur_ms:.2f} ms exceeded 5.0 ms threshold"
        return True, ""
    elif label == "SCHED_WAIT":
        if sched_r == 0 and n_ctx == 0:
            return False, "SCHED_WAIT had zero runqueue delay and zero context switches"
        return True, ""
    elif label == "NEGATIVE_CONTROL":
        return True, ""
    return True, ""

def compute_robust_baselines(durations: list[int]) -> tuple[float, float]:
    if not durations:
        return 0.0, 0.0
    if len(durations) == 1:
        return float(durations[0]), 0.0

    if np is not None:
        med = float(np.median(durations))
        diffs = np.abs(np.array(durations) - med)
        mad = float(np.median(diffs))
    else:
        s_durs = sorted(durations)
        med = float(s_durs[len(s_durs) // 2])
        diffs = sorted([abs(d - med) for d in durations])
        mad = float(diffs[len(diffs) // 2])

    norm_mad = mad * 1.4826
    return med, norm_mad

def correlate_single_run(run_dir: str):
    run_path = Path(run_dir)
    if not run_path.is_dir():
        return

    truth_file = run_path / "truth.jsonl"
    events_file = run_path / "events.csv"
    out_parquet = run_path / "features.parquet"
    out_csv = run_path / "features.csv"
    out_val_json = run_path / "label_validation.json"

    if not truth_file.exists() or not events_file.exists():
        print(f"Skipping {run_dir}: missing truth.jsonl or events.csv")
        return

    # Load ground truth operations
    truth_ops = []
    with open(truth_file, "r") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    truth_ops.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

    if not truth_ops:
        print(f"Skipping {run_dir}: empty truth.jsonl")
        return

    # Pre-compute robust baseline duration (L1) across all operations in this run
    all_durs = [max(int(op["end_ns"]) - int(op["start_ns"]), 1) for op in truth_ops]
    med_dur_ns, mad_dur_ns = compute_robust_baselines(all_durs)

    # Load raw events
    if pd is not None:
        try:
            events_df = pd.read_csv(events_file)
        except Exception as e:
            print(f"Error reading {events_file}: {e}")
            return
        if events_df.empty:
            print(f"Skipping {run_dir}: empty events.csv")
            return
    else:
        with open(events_file, "r", newline="") as f:
            reader = csv.DictReader(f)
            raw_events = list(reader)
        if not raw_events:
            print(f"Skipping {run_dir}: empty events.csv")
            return

    features = []
    validation_records = []

    for op in truth_ops:
        t_start = int(op["start_ns"])
        t_end = int(op["end_ns"])
        tid = int(op["tid"])
        op_duration_ns = max(t_end - t_start, 1)
        run_id = op.get("run_id", run_path.name)
        label = op.get("true_label", op.get("label", "UNKNOWN"))
        norm_label = "CPU_SYSCALL" if label == "CPU_STALL" else label

        if pd is not None:
            # Slice events in time window
            w_events = events_df[(events_df.ts_ns >= t_start) & (events_df.ts_ns <= t_end)]
            t_events = w_events[w_events.tid == tid].sort_values("ts_ns")

            # 1. Block metrics (Layer 3)
            block_events = w_events[w_events.layer == 3]
            block_issues = len(block_events[block_events.type == 0])
            block_completes = len(block_events[block_events.type == 1])
            total_block_bytes = int(block_events[block_events.type == 0]["nr_bytes"].sum()) if not block_events.empty else 0

            # L4: Submitter-attributed block matching (direct vs background)
            target_col = "target_tid" if "target_tid" in block_events.columns else "tid"
            direct_block = block_events[(block_events.tid == tid) | (block_events[target_col] == tid)]
            n_blk_direct = len(direct_block[direct_block.type == 0])
            n_blk_bg = max(block_issues - n_blk_direct, 0)

            # L4: Softirq service time (Layer 4)
            irq_events = w_events[w_events.layer == 4]
            softirq_ns = int(irq_events["ret"].sum()) if not irq_events.empty else 0

            # 2. Scheduler state breakdown (Layer 2)
            sched_events = t_events[t_events.layer == 2]

            on_cpu_ns = 0
            sched_wait_ns = 0
            io_sleep_ns = 0

            last_ts = t_start
            curr_state = "RUNNING"

            for row in sched_events.itertuples():
                dt = max(row.ts_ns - last_ts, 0)
                if curr_state == "RUNNING":
                    on_cpu_ns += dt
                elif curr_state == "RUNNABLE":
                    sched_wait_ns += dt
                elif curr_state == "SLEEP_D":
                    io_sleep_ns += dt

                # State transition
                if row.type == 0:  # switch_out
                    prev_st = int(row.prev_state) & 0xFF
                    if prev_st == 0:
                        curr_state = "RUNNABLE"
                    elif prev_st == 2:
                        curr_state = "SLEEP_D"
                    else:
                        curr_state = "SLEEP_OTHER"
                elif row.type == 1:  # switch_in
                    curr_state = "RUNNING"
                elif row.type == 2:  # wakeup
                    curr_state = "RUNNABLE"

                last_ts = row.ts_ns

            final_dt = max(t_end - last_ts, 0)
            if curr_state == "RUNNING":
                on_cpu_ns += final_dt
            elif curr_state == "RUNNABLE":
                sched_wait_ns += final_dt
            elif curr_state == "SLEEP_D":
                io_sleep_ns += final_dt

            # 3. Syscall counts (Layer 1)
            sys_enters = len(t_events[(t_events.layer == 1) & (t_events.type == 0)])
            n_ctx_switch = len(sched_events[sched_events.type == 0])

        else:
            w_rows = [r for r in raw_events if t_start <= int(r["ts_ns"]) <= t_end]
            t_rows = sorted([r for r in w_rows if int(r["tid"]) == tid], key=lambda x: int(x["ts_ns"]))

            b_rows = [r for r in w_rows if int(r["layer"]) == 3]
            block_issues = sum(1 for r in b_rows if int(r["type"]) == 0)
            block_completes = sum(1 for r in b_rows if int(r["type"]) == 1)
            total_block_bytes = sum(int(r["nr_bytes"]) for r in b_rows if int(r["type"]) == 0)

            direct_issues = sum(1 for r in b_rows if int(r["type"]) == 0 and (int(r.get("target_tid", 0)) == tid or int(r["tid"]) == tid))
            n_blk_direct = direct_issues
            n_blk_bg = max(block_issues - n_blk_direct, 0)

            irq_rows = [r for r in w_rows if int(r["layer"]) == 4]
            softirq_ns = sum(int(r.get("ret", 0)) for r in irq_rows)

            sched_rows = [r for r in t_rows if int(r["layer"]) == 2]
            on_cpu_ns = 0
            sched_wait_ns = 0
            io_sleep_ns = 0

            last_ts = t_start
            curr_state = "RUNNING"

            for r in sched_rows:
                ts = int(r["ts_ns"])
                dt = max(ts - last_ts, 0)
                if curr_state == "RUNNING":
                    on_cpu_ns += dt
                elif curr_state == "RUNNABLE":
                    sched_wait_ns += dt
                elif curr_state == "SLEEP_D":
                    io_sleep_ns += dt

                stype = int(r["type"])
                if stype == 0:
                    pst = int(r["prev_state"]) & 0xFF
                    if pst == 0:
                        curr_state = "RUNNABLE"
                    elif pst == 2:
                        curr_state = "SLEEP_D"
                    else:
                        curr_state = "SLEEP_OTHER"
                elif stype == 1:
                    curr_state = "RUNNING"
                elif stype == 2:
                    curr_state = "RUNNABLE"

                last_ts = ts

            final_dt = max(t_end - last_ts, 0)
            if curr_state == "RUNNING":
                on_cpu_ns += final_dt
            elif curr_state == "RUNNABLE":
                sched_wait_ns += final_dt
            elif curr_state == "SLEEP_D":
                io_sleep_ns += final_dt

            sys_enters = sum(1 for r in t_rows if int(r["layer"]) == 1 and int(r["type"]) == 0)
            n_ctx_switch = sum(1 for r in sched_rows if int(r["type"]) == 0)

        # L3: Time breakdown partitioning: T = t_run + t_runnable + t_blocked
        total_sleep_ns = max(op_duration_ns - on_cpu_ns - sched_wait_ns, 0)
        io_sleep_ns = min(io_sleep_ns, total_sleep_ns)
        other_sleep_ns = total_sleep_ns - io_sleep_ns

        dur_ms = round(op_duration_ns / 1e6, 4)
        on_cpu_ratio = round(min(on_cpu_ns / op_duration_ns, 1.0), 4)
        sched_wait_ratio = round(min(sched_wait_ns / op_duration_ns, 1.0), 4)
        io_sleep_ratio = round(min(io_sleep_ns / op_duration_ns, 1.0), 4)
        sleep_state_frac = round(io_sleep_ns / total_sleep_ns, 4) if total_sleep_ns > 0 else 0.0
        blk_overlap_frac = round(min(io_sleep_ns / op_duration_ns, 1.0), 4) if block_issues > 0 else 0.0

        # L1: Robust spike score
        spike_score = round((op_duration_ns - med_dur_ns) / mad_dur_ns, 3) if mad_dur_ns > 0 else 0.0

        # Diagnostic evidence flags
        ev_flags = 0
        if block_issues > 0:
            ev_flags |= 1
        if sched_wait_ns > 0:
            ev_flags |= 2
        if io_sleep_ns > 0:
            ev_flags |= 4
        if on_cpu_ratio >= 0.75:
            ev_flags |= 8
        if n_blk_direct > 0:
            ev_flags |= 16
        if softirq_ns > 0:
            ev_flags |= 32

        # L6: Label validation
        is_valid, reason = validate_label_signature(
            norm_label, dur_ms, on_cpu_ratio, sched_wait_ratio, io_sleep_ratio, block_issues, n_ctx_switch
        )
        validation_records.append({
            "op_id": op.get("op_id", 0),
            "label": norm_label,
            "valid": is_valid,
            "reason": reason
        })

        features.append({
            "run_id": run_id,
            "op_id": op.get("op_id", 0),
            "tid": tid,
            "duration_ms": dur_ms,
            "dur_ns": op_duration_ns,
            "on_cpu_ratio": on_cpu_ratio,
            "sched_wait_ratio": sched_wait_ratio,
            "io_sleep_ratio": io_sleep_ratio,
            "t_run": on_cpu_ns,
            "t_runnable": sched_wait_ns,
            "t_blocked": total_sleep_ns,
            "t_iowait": io_sleep_ns,
            "t_other_sleep": other_sleep_ns,
            "softirq_ns": softirq_ns,
            "med_dur_ns": med_dur_ns,
            "mad_dur_ns": mad_dur_ns,
            "spike_score": spike_score,
            "block_issues": block_issues,
            "block_completes": block_completes,
            "n_blk_req": block_issues,
            "n_blk_direct": n_blk_direct,
            "n_blk_bg": n_blk_bg,
            "total_block_bytes": total_block_bytes,
            "blk_bytes": total_block_bytes,
            "blk_overlap_frac": blk_overlap_frac,
            "n_ctx_switch": n_ctx_switch,
            "sleep_state_frac": sleep_state_frac,
            "evidence_flags": ev_flags,
            "sys_enters": sys_enters,
            "label_valid": 1 if is_valid else 0,
            "mismatch_reason": reason,
            "true_label": norm_label,
            "label": norm_label
        })

    # Save features to Parquet and CSV
    try:
        if pd is not None:
            feat_df = pd.DataFrame(features)
            try:
                feat_df.to_parquet(out_parquet, index=False)
                print(f"[correlate] Wrote {out_parquet} ({len(feat_df)} operations)")
            except Exception as e:
                print(f"[correlate] Parquet write failed ({e}); falling back to CSV")
            feat_df.to_csv(out_csv, index=False)
            print(f"[correlate] Wrote {out_csv} ({len(feat_df)} operations)")
        else:
            if features:
                keys = list(features[0].keys())
                with open(out_csv, "w", newline="") as f:
                    writer = csv.DictWriter(f, fieldnames=keys)
                    writer.writeheader()
                    writer.writerows(features)
                print(f"[correlate] Wrote {out_csv} ({len(features)} operations)")
    except PermissionError:
        print(f"[correlate] error: permission denied writing features to {run_dir}.")
        print(f"   Fix ownership with: sudo chown -R $USER:$USER {run_dir}")

    # L6: Save label validation report
    n_ops = len(validation_records)
    n_valid = sum(1 for v in validation_records if v["valid"])
    n_invalid = n_ops - n_valid
    noise_rate = round(n_invalid / n_ops, 4) if n_ops > 0 else 0.0

    val_summary = {
        "run_id": run_id,
        "n_ops": n_ops,
        "n_valid": n_valid,
        "n_invalid": n_invalid,
        "label_noise_rate": noise_rate,
        "details": validation_records
    }
    with open(out_val_json, "w") as f:
        json.dump(val_summary, f, indent=2)

    print(f"[correlate] Label validation: {n_valid}/{n_ops} valid (noise rate: {noise_rate*100:.1f}%)")

def main():
    if len(sys.argv) < 2:
        print("Usage: python3 analysis/correlate.py <run_dir_or_glob> [more_dirs...]")
        sys.exit(1)

    targets = []
    for arg in sys.argv[1:]:
        matches = glob.glob(arg)
        if matches:
            targets.extend(matches)
        else:
            targets.append(arg)

    for target in targets:
        correlate_single_run(target)

if __name__ == "__main__":
    main()
