#!/usr/bin/env python3
"""
Lightweight Machine Learning Baseline Classifier for eBPF Stall Attribution.

Key requirements:
1. Strict Run-Level Split: Prevents data leakage between train and test operations
   from the same hardware/cache state.
2. Abstention via Probability Thresholding: Enforces H2 (Indeterminate class when
   max predicted probability is below confidence threshold tau).
3. Interpretable Models: DecisionTreeClassifier (depth <= 4) or LogisticRegression.
"""

import sys
import os
import json
import argparse
import glob
from pathlib import Path

try:
    import pandas as pd
    import numpy as np
    from sklearn.tree import DecisionTreeClassifier, export_text
    from sklearn.linear_model import LogisticRegression
    from sklearn.model_selection import GroupKFold
    from sklearn.metrics import classification_report, accuracy_score
except ImportError as e:
    pd = None
    np = None
    print(f"Warning: ML dependencies not fully available: {e}")

FEATURE_COLS = [
    "duration_ms",
    "on_cpu_ratio",
    "sched_wait_ratio",
    "io_sleep_ratio",
    "block_issues",
    "block_completes",
    "total_block_bytes",
    "sys_enters",
    "n_ctx_switch"
]

def load_dataset(directories):
    all_dfs = []
    for d in directories:
        p = Path(d)
        parquet_file = p / "features.parquet"
        csv_file = p / "features.csv"

        df = None
        if parquet_file.exists():
            try:
                df = pd.read_parquet(parquet_file)
            except Exception:
                pass

        if df is None and csv_file.exists():
            try:
                df = pd.read_csv(csv_file)
            except Exception:
                pass

        if df is not None and not df.empty:
            if "run_id" not in df.columns:
                df["run_id"] = p.name
            df["_source_dir"] = str(p)
            all_dfs.append(df)

    if not all_dfs:
        return pd.DataFrame()

    combined = pd.concat(all_dfs, ignore_index=True)
    # Normalize labels: harmonize CPU_STALL and CPU_SYSCALL
    if "true_label" in combined.columns:
        combined["normalized_label"] = combined["true_label"].replace({"CPU_STALL": "CPU_SYSCALL"})
    elif "label" in combined.columns:
        combined["normalized_label"] = combined["label"].replace({"CPU_STALL": "CPU_SYSCALL"})
    else:
        combined["normalized_label"] = "UNKNOWN"

    # Fill any missing feature columns with 0
    for col in FEATURE_COLS:
        if col not in combined.columns:
            combined[col] = 0.0
        combined[col] = combined[col].fillna(0.0)

    return combined

def train_and_evaluate(df, model_type="tree", threshold=0.60, max_depth=4):
    if df.empty or len(df) < 2:
        print("Insufficient samples for training. Need at least 2 distinct operations.")
        return None

    X = df[FEATURE_COLS].values
    y = df["normalized_label"].values
    groups = df["run_id"].values

    unique_labels = np.unique(y)
    unique_runs = np.unique(groups)

    print(f"Dataset summary: {len(df)} samples across {len(unique_runs)} runs. Classes: {unique_labels}")

    if model_type == "tree":
        model = DecisionTreeClassifier(max_depth=max_depth, random_state=42)
    else:
        model = LogisticRegression(max_iter=500, random_state=42)

    # If multiple runs exist, perform cross-validation with GroupKFold
    n_splits = min(len(unique_runs), 3)
    if n_splits > 1:
        gkf = GroupKFold(n_splits=n_splits)
        fold_accs = []
        for fold, (train_idx, test_idx) in enumerate(gkf.split(X, y, groups=groups)):
            X_tr, y_tr = X[train_idx], y[train_idx]
            X_te, y_te = X[test_idx], y[test_idx]

            # Ensure all classes in test exist in train for fold
            clf = DecisionTreeClassifier(max_depth=max_depth, random_state=42) if model_type == "tree" else LogisticRegression(max_iter=500, random_state=42)
            clf.fit(X_tr, y_tr)
            preds = clf.predict(X_te)
            acc = accuracy_score(y_te, preds)
            fold_accs.append(acc)

        print(f"Cross-Validation Group-KFold ({n_splits} folds) Mean Accuracy: {np.mean(fold_accs):.2f}")

    # Fit final model on all data
    model.fit(X, y)

    if model_type == "tree":
        print("\n--- Trained Decision Tree Rules ---")
        try:
            tree_rules = export_text(model, feature_names=FEATURE_COLS)
            print(tree_rules)
        except Exception:
            pass

        print("\n--- Feature Importances ---")
        importances = model.feature_importances_
        sorted_indices = np.argsort(importances)[::-1]
        for idx in sorted_indices:
            if importances[idx] > 0.001:
                print(f"  {FEATURE_COLS[idx]:<20}: {importances[idx]:.4f}")

    return model

def predict_with_abstention(model, df, threshold=0.60):
    X = df[FEATURE_COLS].values
    probs = model.predict_proba(X)
    classes = model.classes_

    predictions = []
    for i, row in df.iterrows():
        prob_vec = probs[i]
        max_idx = int(np.argmax(prob_vec))
        max_prob = float(prob_vec[max_idx])
        pred_label = classes[max_idx]

        # Enforce abstention threshold
        if max_prob < threshold:
            final_label = "INDETERMINATE"
        else:
            final_label = pred_label

        if final_label == "IO_WAIT":
            est_stall_ns = int(row.get("t_blocked", 0))
        elif final_label == "SCHED_WAIT":
            est_stall_ns = int(row.get("t_runnable", 0))
        else:
            est_stall_ns = 0

        rec = {
            "run_id": str(row.get("run_id", "default_run")),
            "op_id": int(row.get("op_id", 0)),
            "pred": final_label,
            "conf": round(max_prob, 3),
            "method": "decision_tree" if isinstance(model, DecisionTreeClassifier) else "logistic_regression",
            "est_stall_ns": est_stall_ns,
            "pred_label": final_label,
            "confidence": round(max_prob, 3),
            "tid": int(row.get("tid", 0)),
            "true_label": str(row.get("normalized_label", "UNKNOWN")),
            "_source_dir": row.get("_source_dir", "")
        }
        predictions.append(rec)

    return predictions

def write_predictions_by_dir(predictions, out_filename="pred_tree.jsonl"):
    grouped = {}
    for p in predictions:
        src = p.get("_source_dir")
        if src:
            grouped.setdefault(src, []).append(p)

    for src_dir, preds in grouped.items():
        out_path = Path(src_dir) / out_filename
        try:
            with open(out_path, "w") as f:
                for p in preds:
                    clean = {k: v for k, v in p.items() if not k.startswith("_")}
                    f.write(json.dumps(clean) + "\n")
            print(f"[model] Wrote {len(preds)} predictions to {out_path}")
        except PermissionError:
            print(f"[model] error: permission denied writing {out_path}.")
            print(f"   Fix ownership with: sudo chown -R $USER:$USER {src_dir}")

def main():
    parser = argparse.ArgumentParser(description="Lightweight ML Classifier for eBPF Stalls with Abstention")
    parser.add_argument("paths", nargs="+", help="Run directories or glob patterns (e.g. data/tune/*)")
    parser.add_argument("--model", choices=["tree", "logistic"], default="tree", help="Classifier type")
    parser.add_argument("--threshold", type=float, default=0.60, help="Abstention threshold tau (default: 0.60)")
    parser.add_argument("--max-depth", type=int, default=4, help="Max depth for DecisionTree")
    parser.add_argument("--out-name", default="pred_tree.jsonl", help="Output filename in each run dir")

    args = parser.parse_args()

    if pd is None:
        print("Error: pandas and scikit-learn are required to run analysis/model.py")
        sys.exit(1)

    matched_dirs = []
    for p in args.paths:
        matches = glob.glob(p)
        if matches:
            matched_dirs.extend([m for m in matches if Path(m).is_dir()])
        elif Path(p).is_dir():
            matched_dirs.append(p)

    if not matched_dirs:
        print("No valid run directories found.")
        sys.exit(1)

    print(f"Loading data from {len(matched_dirs)} directories...")
    df = load_dataset(matched_dirs)
    if df.empty:
        print("No feature records found in given directories.")
        sys.exit(1)

    model = train_and_evaluate(df, model_type=args.model, threshold=args.threshold, max_depth=args.max_depth)
    if model is None:
        sys.exit(1)

    preds = predict_with_abstention(model, df, threshold=args.threshold)
    write_predictions_by_dir(preds, out_filename=args.out_name)

if __name__ == "__main__":
    main()

