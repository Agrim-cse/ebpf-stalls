# eBPF-Based System Stall Detection and Analysis

An Operating Systems course project for observing and classifying application
latency using Linux eBPF tracepoints. The project generates controlled
microbenchmark operations, records kernel telemetry around each operation, and
compares measured signals with harness-provided ground truth.

## 1. Project Overview

A slow operation does not, by itself, reveal why it was slow. A thread may be
actively computing, runnable but waiting for a CPU, blocked in uninterruptible
sleep while storage completes an I/O, or executing a fast read satisfied by the
page cache. This project explores whether those cases can be distinguished by
combining signals from multiple kernel layers rather than relying on a single
latency or syscall indicator.

The current taxonomy includes:

| Label | Intended meaning | Typical evidence |
|---|---|---|
| `CPU_SYSCALL` | A compute-bound interval such as the harness spin loop | High on-CPU time, no block requests |
| `SCHED_WAIT` | Runnable work delayed by CPU contention | Scheduler switch/wakeup events and runnable delay |
| `IO_WAIT` | A synchronous storage operation that blocks for I/O | Uninterruptible (`D`-state) sleep overlapping block I/O |
| `WARM_READ` | A buffered read expected to hit the page cache | Short read with no physical block request |
| `INDETERMINATE` | Mixed, conflicting, or insufficient evidence | Abstain instead of forcing a single cause |

The main experimental questions, described in
[`docs/taxonomy.md`](docs/taxonomy.md), are whether syscall, scheduler, and
block-layer telemetry improves classification over isolated signals, and
whether an explicit abstention class reduces erroneous attribution.

## 2. Architecture & Components

The collection and analysis workflow is:

1. **Build the collector and workload harness.** The root `Makefile` delegates
   to `tracer/` and `workloads/`.
2. **Choose and launch a run.** `workloads/run_matrix.py` selects configurations
   and calls `workloads/run_scenario.sh`. The scenario script prepares test
   files, records run metadata, and starts the collector around the harness.
3. **Arm eBPF before the workload starts.** `tracer/collector` forks the target
   command and holds the child at a pipe barrier while it loads and attaches
   the BPF programs, registers the target process, and creates the ring-buffer
   consumer. It then releases the child.
4. **Capture ground truth and kernel events.** `workloads/harness` writes
   operation windows and labels to `truth.jsonl`. The collector writes raw
   trace events to `events.csv` and its ring-buffer drop count to `drops.txt`.
5. **Correlate and classify.** `analysis/correlate.py` slices events to each
   ground-truth interval and produces per-operation features. The rule and
   model scripts create predictions. `eval/` compares those predictions with
   ground truth and generates metrics and plots.

### Tracer

[`tracer/bpf/trace.bpf.c`](tracer/bpf/trace.bpf.c) attaches to four event
families:

- **Syscalls (layer 1):** `raw_syscalls/sys_enter` and `sys_exit`.
- **Scheduler (layer 2):** `sched_switch` and `sched_wakeup`, including
  normalized runnable, interruptible-sleep, and uninterruptible-sleep states.
- **Block I/O (layer 3):** bio queue, request issue, and request completion.
  The `bio_submitters` map records the submitting TID so work dispatched or
  completed by kernel workers can be associated with its originating thread.
- **SoftIRQ (layer 4):** entry and exit timing for softirqs observed while a
  tracked thread is executing.

Events share a fixed-size record and a 64 MiB BPF ring buffer. Failed ring
buffer reservations increment a drop counter, which the collector saves at
the end of the run. The collector serializes records into the CSV schema
documented in [`docs/schema.md`](docs/schema.md).

### Workload harness and orchestration

[`workloads/harness.c`](workloads/harness.c) provides `cpu`, `io`, `warm`,
`sched`, and `mixed` modes. It records monotonic start/end timestamps, TID,
operation ID, byte count where relevant, and the expected label in JSONL.
[`workloads/run_scenario.sh`](workloads/run_scenario.sh) prepares inputs,
writes `run.json`, and invokes the collector and harness together.

The intended configuration catalogue is
[`workloads/scenarios.yaml`](workloads/scenarios.yaml); the background-I/O
helper is [`workloads/negative_controls/fio_bg.sh`](workloads/negative_controls/fio_bg.sh).
See [Usage](#6-usage) for an important distinction: the current matrix runner
does not parse that YAML or automatically launch the `fio` control.

### Analysis and evaluation

- `analysis/correlate.py` joins operation windows with raw trace events. It
  calculates on-CPU, runnable, sleep, block-I/O, and softirq features; writes
  `features.csv` and, when supported, `features.parquet`; and records label
  validation in `label_validation.json`.
- `analysis/rules.py` applies thresholds from `analysis/thresholds.json` and
  writes `pred_rules.jsonl`.
- `analysis/model.py` fits a decision tree or logistic-regression classifier,
  applies confidence-based abstention, and writes per-run prediction JSONL.
- `eval/metrics.py`, `baselines.py`, and `ablations.py` compare classifiers and
  observability choices. `eval/overhead.py` measures a baseline versus a
  traced workload. Figures are written under `eval/plots/`.

## 3. Repository Structure

```text
.
├── Makefile                         # Builds/cleans tracer and harness
├── tracer/
│   ├── bpf/                         # eBPF probes and shared event definitions
│   ├── src/collector.c              # Loads BPF, launches target, writes CSV
│   ├── tests/                       # Direct-I/O helper and run validator
│   ├── Makefile                     # Generates vmlinux.h/skeleton and binaries
│   └── test.csv                     # Older sample event data
├── workloads/
│   ├── harness.c                    # CPU, I/O, scheduler, and mixed workloads
│   ├── run_matrix.py                # Current hard-coded run matrix
│   ├── run_scenario.sh              # Per-run setup and collection
│   ├── scenarios.yaml               # Broader declarative scenario catalogue
│   └── negative_controls/fio_bg.sh # Optional background storage noise
├── analysis/
│   ├── correlate.py                 # Raw events + ground truth → features
│   ├── rules.py                     # Threshold-based classifier
│   ├── model.py                     # Tree/logistic classifier
│   └── thresholds.json              # Rule and abstention thresholds
├── eval/
│   ├── metrics.py                   # Classification metrics and confusion plots
│   ├── baselines.py                 # Single-layer and fused comparisons
│   ├── ablations.py                 # Layer/window/attribution studies
│   ├── overhead.py                  # Instrumentation overhead measurement
│   └── plots/                       # Generated or included evaluation figures
├── env/                             # Captured kernel, CPU, compiler, and tool info
├── docs/
│   ├── schema.md                    # Data contracts and event encodings
│   ├── taxonomy.md                  # Stall definitions and hypotheses
│   └── report/                      # Architecture and evaluation reports
└── test_events.csv                  # Currently empty; not used by the pipeline
```

Generated BPF artifacts and executables are ignored by Git. Run data is written
under `data/`, which is also ignored; preserve or back it up separately if it
must be retained.

## 4. Prerequisites

### Platform and privileges

- A **native Linux host** with eBPF, BTF, tracepoints, and tracefs support. The
  checked-in environment snapshot records Ubuntu 22.04, x86_64, and Linux
  `6.8.0-138-generic`. The BPF build currently selects the x86 target, so other
  architectures require build changes.
- Kernel BTF available at `/sys/kernel/btf/vmlinux`.
- Root privileges or suitable capabilities to load/attach BPF programs. The
  workload matrix invokes scenario runs with `sudo`.
- A filesystem that supports `O_DIRECT` for the cold-read workload.
- A controlled test machine: cold-I/O setup may write a 128 MiB scratch file
  and attempt to drop the system page cache. These operations can affect other
  workloads. The optional `fio` control writes a separate 512 MiB file and
  intentionally adds disk traffic.

Windows is not a supported execution host for these Linux tracepoints and
shell scripts. A Linux VM or WSL kernel is suitable only if it exposes the
required BTF, tracefs, BPF capabilities, and block tracepoints; the project
environment report describes native Linux testing.

### Build tools

- GNU Make and a C compiler (`gcc`/`cc`)
- Clang/LLVM with the BPF target
- `bpftool`
- libbpf 1.4-compatible headers and the static archive `libbpf.a`
- libelf and zlib development libraries
- Bash and common Linux utilities used by the scripts: `sudo`, `dd`, `sync`,
  `df`, `getent`, `awk`, and `chown`
- `fio` only for the optional background-I/O negative control

The tracer `Makefile` expects the libbpf installation to contain `include/`
and `lib64/libbpf.a`. It searches for `libbpf-install` at the repository root
or in the user's home directory; set `LIBBPF` explicitly if it is elsewhere.
The Makefile does not fetch or build libbpf for you.

### Python dependencies

The repository does not include a Python dependency manifest. Python 3 is
needed for orchestration and analysis. The core analysis/evaluation scripts
use NumPy, pandas, scikit-learn, and Matplotlib; `eval/overhead.py` also uses
psutil. PyArrow enables Parquet output, while correlation and rule scripts have
CSV paths when Parquet support is unavailable.

For example, in a Linux virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install numpy pandas scikit-learn matplotlib psutil pyarrow
```

`PyYAML` is not currently required by the runner because it does not load
`scenarios.yaml`.

## 5. Build & Setup

Run these commands from the repository root on the Linux host.

1. **Install prerequisites** listed above, including a static libbpf
   installation. Ensure `/sys/kernel/btf/vmlinux` exists.
2. **Build both components:**

   ```bash
   make
   ```

   This invokes `make -C tracer` followed by `make -C workloads`. The tracer
   build generates `tracer/vmlinux.h` from the running kernel's BTF, compiles
   the eBPF object, generates the libbpf skeleton, and links the collector. It
   also builds `tracer/tests/test_direct_io`. The workloads build produces
   `workloads/harness`.

   If libbpf is installed outside the default locations:

   ```bash
   make LIBBPF=/path/to/libbpf-install
   ```

3. **Check the generated executables:**

   ```bash
   test -x tracer/collector
   test -x workloads/harness
   ```

4. **Optionally inspect the matrix without running it:**

   ```bash
   python3 workloads/run_matrix.py --split tune --dry-run
   ```

To remove generated executables and BPF build intermediates:

```bash
make clean
```

`make clean` does not remove run data under `data/` or the generated
`tracer/vmlinux.h`.

## 6. Usage

### Collect workload telemetry

From the repository root:

```bash
python3 workloads/run_matrix.py --split tune --dry-run
python3 workloads/run_matrix.py --split tune
```

The runner defaults to `tune`. To run the same built-in matrix into the
`final` output directory:

```bash
python3 workloads/run_matrix.py --split final
```

For a quick, limited run:

```bash
python3 workloads/run_matrix.py --split tune --max-runs 1
```

The current runner contains **nine hard-coded configurations**: two CPU spin
durations, two direct-I/O block sizes, two warm-read block sizes, two scheduler
competitor counts, and one mixed I/O/scheduler case. It runs five iterations
for most cases and three for the mixed case. Each completed run is stored in:

```text
data/<split>/<run-id>/
├── run.json       # Workload, cache, device, kernel, and split metadata
├── truth.jsonl    # Harness operation windows and expected labels
├── events.csv     # Raw events emitted by the collector
└── drops.txt      # Ring-buffer event-drop count
```

**Configuration caveat:** `workloads/scenarios.yaml` describes additional
factor combinations, seeds, and a negative-control scenario, but
`run_matrix.py` does not read it. Editing the YAML alone will not change what
gets executed. Likewise, the matrix does not call `fio_bg.sh`; start that
helper separately if you intentionally want background I/O, and account for
the fact that it runs asynchronously.

### Correlate, classify, and evaluate

After collecting runs, execute the analysis stages in order:

```bash
# Create per-operation features and label-validation reports.
python3 analysis/correlate.py "data/tune/*"

# Generate rule-based predictions and model predictions.
python3 analysis/rules.py "data/tune/*"
python3 analysis/model.py "data/tune/*" --model tree --threshold 0.60

# Evaluate the two predictions.
python3 eval/metrics.py data/tune rules
python3 eval/metrics.py data/tune tree

# Generate single-layer baseline predictions and comparative evaluation.
python3 eval/baselines.py data/tune

# Run layer/window/attribution ablation routines.
python3 eval/ablations.py data/tune
```

The correlation step writes `features.csv`, optionally `features.parquet`, and
`label_validation.json` into each run directory. Classification scripts write
`pred_rules.jsonl`, `pred_tree.jsonl` (by default), and baseline prediction
files there. Evaluation metrics are printed to the terminal; confusion
matrices and comparison figures are saved under `eval/plots/`.

To measure tracing overhead separately, build the collector first and run on
the Linux host:

```bash
sudo python3 eval/overhead.py
```

This benchmark runs a warm-read workload with and without tracing and writes
an overhead plot. It is an experiment, not part of `run_matrix.py`.

### Reading the raw event CSV

The collector writes the following header to each run's `events.csv`:

```text
ts_ns,pid,tid,layer,type,syscall_nr,ret,prev_state,sector,dev,nr_bytes,cpu,target_tid,rwbs
```

`ts_ns` is monotonic nanoseconds, matching the harness' `start_ns` and
`end_ns`. `layer` identifies syscall (1), scheduler (2), block I/O (3), or
IRQ/SoftIRQ (4); `type` is specific to the layer. The full value definitions
are in [`docs/schema.md`](docs/schema.md). Each run has a separate CSV, so the
collector does not add a `run_id` column to the CSV itself.

## Reproducibility and Implementation Notes

- **YAML is not wired into execution.** The YAML catalogue and the runner's
  built-in matrix currently differ. The `fio` negative control is also not
  automatically orchestrated.
- **Final-split model workflow is not fully implemented.** The runner can
  collect `data/final`, but `analysis/model.py` trains and predicts on the
  directories supplied to it; it has no separate train-on-tune/predict-on-final
  interface. Do not treat its in-sample predictions as a held-out final test
  without adding that workflow.
- **Legacy/test CSVs are not the current run output.** The root
  `test_events.csv` is empty, and `tracer/test.csv` has an older, shorter
  schema. Use the per-run `events.csv` files produced by the collector.
- **The run validator has legacy paths.** `tracer/tests/validate_runs.py`
  checks fixed names such as `data/tune/test_cpu`, which do not match the
  default run IDs created by the current matrix. It may need to be pointed at
  or updated for the generated run directories.
- **The matrix creates privileged, system-wide effects.** Cold-read setup
  attempts to flush caches, scheduler modes pin work to CPU 0, and block
  tracepoints may observe system-wide block activity. Run experiments on an
  otherwise controlled machine and inspect `drops.txt` before trusting a
  result.
- Results depend on kernel version, CPU topology, storage device, filesystem,
  cache state, and background system activity. The snapshots under `env/` and
  methodology in `docs/report/` document the recorded test environment; they
  are not a guarantee of identical behavior on other hosts.

## Further Documentation

- [`docs/taxonomy.md`](docs/taxonomy.md) — stall taxonomy and hypotheses
- [`docs/schema.md`](docs/schema.md) — event, ground-truth, and feature schemas
- [`docs/report/tracer_architecture.md`](docs/report/tracer_architecture.md) —
  collector and eBPF design
- [`docs/report/sec_eval.md`](docs/report/sec_eval.md) — evaluation methodology
- [`env/`](env/) — captured system and toolchain details
