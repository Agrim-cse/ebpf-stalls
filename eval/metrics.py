#!/usr/bin/env python3
"""
Multi-Class Metrics & Confusion Matrix Evaluation Engine.

Computes:
- Macro-F1, Precision, Recall, and per-class metrics
- Explicit Abstention Rate (INDETERMINATE %)
- Multi-layer Confusion Matrix (Ground Truth vs Predictions + Abstain)
- Publication-quality confusion matrix heatmaps saved to eval/plots/
"""

import sys
import json
import glob
from pathlib import Path
import numpy as np
import pandas as pd

import matplotlib
matplotlib.use("Agg")  # Non-interactive headless backend
import matplotlib.pyplot as plt
from sklearn.metrics import classification_report, confusion_matrix, f1_score, precision_score, recall_score

CANONICAL_CLASSES = ["CPU_SYSCALL", "IO_WAIT", "SCHED_WAIT", "WARM_READ"]
ALL_PRED_CLASSES = CANONICAL_CLASSES + ["INDETERMINATE"]

def normalize_class(label: str) -> str:
    label = str(label).strip()
    if label == "CPU_STALL":
        return "CPU_SYSCALL"
    return label

def compute_bootstrap_ci(y_true: list, y_pred: list, n_bootstraps: int = 1000, alpha: float = 0.05) -> tuple[float, float, float]:
    """Computes bootstrap mean and 95% confidence interval for Macro-F1."""
    if len(y_true) < 2:
        score = f1_score(y_true, y_pred, labels=CANONICAL_CLASSES, average="macro", zero_division=0)
        return score, score, score

    boot_scores = []
    rng = np.random.default_rng(42)
    n = len(y_true)
    for _ in range(n_bootstraps):
        idx = rng.integers(0, n, size=n)
        s_true = [y_true[i] for i in idx]
        s_pred = [y_pred[i] for i in idx]
        s_score = f1_score(s_true, s_pred, labels=CANONICAL_CLASSES, average="macro", zero_division=0)
        boot_scores.append(s_score)

    f1_mean = float(np.mean(boot_scores))
    ci_low = float(np.percentile(boot_scores, 100.0 * (alpha / 2.0)))
    ci_high = float(np.percentile(boot_scores, 100.0 * (1.0 - alpha / 2.0)))
    return f1_mean, ci_low, ci_high

def evaluate_predictions(runs_dir: str, pred_suffix: str = "rules", save_plot: bool = True):
    truth_records = []
    pred_records = []
    run_records = []
    stall_errors_ms = []

    target_path = Path(runs_dir)
    # Collect candidate run directories
    if target_path.is_dir():
        # Check if runs_dir itself is a single run dir or parent
        if (target_path / "truth.jsonl").exists():
            candidate_dirs = [target_path]
        else:
            candidate_dirs = [p for p in target_path.glob("**/") if (p / "truth.jsonl").exists()]
    else:
        # Glob match
        matches = glob.glob(runs_dir)
        candidate_dirs = [Path(m) for m in matches if Path(m).is_dir() and (Path(m) / "truth.jsonl").exists()]

    for run_path in sorted(candidate_dirs):
        truth_file = run_path / "truth.jsonl"
        pred_file = run_path / f"pred_{pred_suffix}.jsonl"

        if not truth_file.exists() or not pred_file.exists():
            continue

        truths = {}
        with open(truth_file, "r") as tf:
            for line in tf:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    op_id = int(obj.get("op_id", 0))
                    lbl = normalize_class(obj.get("true_label", obj.get("label", "UNKNOWN")))
                    truths[op_id] = lbl
                except json.JSONDecodeError:
                    pass

        preds = {}
        with open(pred_file, "r") as pf:
            for line in pf:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    op_id = int(obj.get("op_id", 0))
                    plbl = normalize_class(obj.get("pred_label", obj.get("pred", "INDETERMINATE")))
                    preds[op_id] = plbl
                except json.JSONDecodeError:
                    pass

        for op_id, true_label in truths.items():
            if op_id in preds:
                truth_records.append(true_label)
                pred_records.append(preds[op_id])
                run_records.append(run_path.name)

    if not truth_records:
        print(f"No matched records found for evaluation in '{runs_dir}' with suffix '{pred_suffix}'.")
        return None

    df = pd.DataFrame({"run": run_records, "true": truth_records, "pred": pred_records})
    total_ops = len(df)
    abstained = (df["pred"] == "INDETERMINATE").sum()
    abstention_rate = (abstained / total_ops) * 100.0

    print(f"\n==========================================")
    print(f" Evaluation Summary ({pred_suffix})")
    print(f"==========================================")
    print(f"Target Directory:  {runs_dir}")
    print(f"Total Operations:  {total_ops}")
    print(f"Decided Cases:     {total_ops - abstained} ({(total_ops - abstained)/total_ops*100:.1f}%)")
    print(f"Abstentions:       {abstained} ({abstention_rate:.2f}%)")

    # Metrics on decided cases
    decided_df = df[df["pred"] != "INDETERMINATE"]
    macro_f1 = 0.0
    if not decided_df.empty:
        print("\n--- Classification Report (Decided Instances) ---")
        report = classification_report(decided_df["true"], decided_df["pred"],
                                       labels=CANONICAL_CLASSES, zero_division=0)
        print(report)
        macro_f1 = f1_score(decided_df["true"], decided_df["pred"],
                            labels=CANONICAL_CLASSES, average="macro", zero_division=0)
        f1_boot, ci_low, ci_high = compute_bootstrap_ci(decided_df["true"].tolist(), decided_df["pred"].tolist())
        print(f"Macro-F1 (Decided): {macro_f1:.4f} (95% Bootstrap CI: [{ci_low:.4f}, {ci_high:.4f}])")
    else:
        ci_low, ci_high = 0.0, 0.0
        print("\nAll cases were abstained as INDETERMINATE.")

    # Multi-class Confusion Matrix (Known true classes vs All predicted classes + INDETERMINATE)
    cm = confusion_matrix(df["true"], df["pred"], labels=ALL_PRED_CLASSES)
    cm_display = cm[:len(CANONICAL_CLASSES), :]  # Rows: True classes

    cm_df = pd.DataFrame(
        cm_display,
        index=[f"True_{c}" for c in CANONICAL_CLASSES],
        columns=[f"Pred_{c}" for c in ALL_PRED_CLASSES]
    )
    print("\n--- Confusion Matrix ---")
    print(cm_df)

    # Plot Confusion Matrix
    if save_plot:
        plot_dir = Path("eval/plots")
        plot_dir.mkdir(parents=True, exist_ok=True)
        plot_path = plot_dir / f"cm_{pred_suffix}.png"

        fig, ax = plt.subplots(figsize=(8, 6))
        cax = ax.imshow(cm_display, interpolation="nearest", cmap="Blues")
        fig.colorbar(cax)

        ax.set_xticks(range(len(ALL_PRED_CLASSES)))
        ax.set_yticks(range(len(CANONICAL_CLASSES)))
        ax.set_xticklabels(ALL_PRED_CLASSES, rotation=35, ha="right", fontsize=10)
        ax.set_yticklabels(CANONICAL_CLASSES, fontsize=10)

        # Annotate cell values
        thresh = cm_display.max() / 2.0 if cm_display.max() > 0 else 1.0
        for i in range(len(CANONICAL_CLASSES)):
            for j in range(len(ALL_PRED_CLASSES)):
                val = cm_display[i, j]
                ax.text(j, i, format(val, "d"),
                        ha="center", va="center",
                        color="white" if val > thresh else "black",
                        fontsize=12, fontweight="bold")

        plt.xlabel("Predicted Label", fontsize=11, labelpad=8)
        plt.ylabel("Ground Truth Label", fontsize=11, labelpad=8)
        plt.title(f"Stall Attribution Confusion Matrix\nMethod: {pred_suffix} (Macro-F1: {macro_f1:.3f}, Abstain: {abstention_rate:.1f}%)",
                  fontsize=12, pad=15)
        plt.tight_layout()
        plt.savefig(plot_path, dpi=200)
        plt.close(fig)
        print(f"\nSaved confusion matrix plot to {plot_path}")

    return {
        "method": pred_suffix,
        "total_ops": total_ops,
        "abstained": int(abstained),
        "abstention_rate": float(abstention_rate),
        "macro_f1": float(macro_f1),
        "ci_95": [float(ci_low), float(ci_high)],
        "confusion_matrix": cm_display.tolist()
    }

def main():
    target_dir = sys.argv[1] if len(sys.argv) > 1 else "data/tune"
    suffix = sys.argv[2] if len(sys.argv) > 2 else "rules"
    evaluate_predictions(target_dir, suffix)

if __name__ == "__main__":
    main()

