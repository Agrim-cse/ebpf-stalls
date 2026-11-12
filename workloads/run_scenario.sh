#!/usr/bin/env bash
set -e

if [ "$#" -lt 3 ]; then
    echo "Usage: sudo $0 <output_dir> <mode> <param> [block_size]"
    echo "Modes: io | warm | cpu | sched"
    echo "Examples:"
    echo "  sudo $0 data/tune/run_cold_io io ~/ebpf_lab/scratch/test.bin 1048576"
    echo "  sudo $0 data/tune/run_warm_io warm ~/ebpf_lab/scratch/test.bin 1048576"
    echo "  sudo $0 data/tune/run_cpu cpu 50"
    echo "  sudo $0 data/tune/run_sched sched 4"
    exit 1
fi

OUT_DIR="$1"
MODE="$2"
PARAM="$3"
BS="${4:-1048576}"
ITERS="${5:-1}"

# 1. Resolve repository root and binary paths
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
COLLECTOR="$REPO_ROOT/tracer/collector"
HARNESS="$REPO_ROOT/workloads/harness"

# 2. Resolve user home (handles sudo invocation correctly)
if [ -n "$SUDO_USER" ]; then
    USER_HOME="$(getent passwd "$SUDO_USER" | cut -d: -f6)"
else
    USER_HOME="$HOME"
fi

SCRATCH_DIR="${SCRATCH_DIR:-$USER_HOME/ebpf_lab/scratch}"
mkdir -p "$SCRATCH_DIR"

# Expand tilde in PARAM if passed as a string
PARAM="${PARAM/#\~/$USER_HOME}"

# 3. Setup output files inside OUT_DIR
mkdir -p "$OUT_DIR"
RUN_ID="$(basename "$OUT_DIR")"
TRUTH_FILE="$OUT_DIR/truth.jsonl"
EVENTS_FILE="$OUT_DIR/events.csv"
DROPS_FILE="$OUT_DIR/drops.txt"
rm -f "$TRUTH_FILE" "$EVENTS_FILE" "$DROPS_FILE"

# 4. Handle cache state & test files for I/O workloads
if [ "$MODE" == "io" ]; then
    # Ensure directory for target file exists
    mkdir -p "$(dirname "$PARAM")"
    if [ ! -f "$PARAM" ]; then
        echo "[run_scenario] Creating cold I/O scratch file: $PARAM (128 MB)..."
        dd if=/dev/urandom of="$PARAM" bs=1M count=128 oflag=direct status=none
    fi
    # Cold read setup: sync and flush kernel page cache
    sync
    if [ -w /proc/sys/vm/drop_caches ]; then
        echo 3 > /proc/sys/vm/drop_caches
    fi
elif [ "$MODE" == "warm" ]; then
    mkdir -p "$(dirname "$PARAM")"
    if [ ! -f "$PARAM" ]; then
        echo "[run_scenario] Creating warm I/O scratch file: $PARAM (128 MB)..."
        dd if=/dev/urandom of="$PARAM" bs=1M count=128 status=none
    fi
    # Populate page cache beforehand
    cat "$PARAM" > /dev/null 2>&1 || true
fi

# 5. Record run metadata (manifest)
SPLIT="tune"
if [[ "$OUT_DIR" =~ /final/ ]]; then
    SPLIT="final"
fi

CACHE_STATE="none"
IO_MODE="none"
if [ "$MODE" == "io" ]; then
    CACHE_STATE="cold"
    IO_MODE="O_DIRECT"
elif [ "$MODE" == "warm" ]; then
    CACHE_STATE="warm"
    IO_MODE="buffered"
fi

CPU_CONTENTION=false
if [ "$MODE" == "sched" ]; then
    CPU_CONTENTION=true
fi

SEED="${SEED:-42}"
CONCURRENCY="${CONCURRENCY:-1}"
TARGET_DEV="$(df -P "$SCRATCH_DIR" 2>/dev/null | tail -1 | awk '{print $1}')"
TARGET_DEV="${TARGET_DEV:-unknown}"

cat > "$OUT_DIR/run.json" <<EOF
{
  "run_id": "$RUN_ID",
  "workload": "$MODE",
  "io_mode": "$IO_MODE",
  "file_size_mb": $(($BS / 1048576)),
  "concurrency": $CONCURRENCY,
  "cache_state": "$CACHE_STATE",
  "cpu_contention": $CPU_CONTENTION,
  "seed": $SEED,
  "kernel": "$(uname -r)",
  "device": "$TARGET_DEV",
  "split": "$SPLIT"
}
EOF

# 6. Ensure binaries exist
if [ ! -x "$COLLECTOR" ]; then
    echo "[run_scenario] Building collector..."
    make -C "$REPO_ROOT/tracer"
fi

if [ ! -x "$HARNESS" ]; then
    echo "[run_scenario] Building harness..."
    gcc -O2 -Wall -pthread "$REPO_ROOT/workloads/harness.c" -o "$HARNESS"
fi

# 7. Launch Teammate 2's collector wrapping our harness execution
export RUN_ID="$RUN_ID"
echo "Executing: $COLLECTOR -r $RUN_ID -o $EVENTS_FILE -d $DROPS_FILE -- $HARNESS $MODE $TRUTH_FILE $PARAM $BS $ITERS"
"$COLLECTOR" -r "$RUN_ID" -o "$EVENTS_FILE" -d "$DROPS_FILE" -- "$HARNESS" "$MODE" "$TRUTH_FILE" "$PARAM" "$BS" "$ITERS"

echo "Run completed. Output generated in: $OUT_DIR"
echo "  - Metadata: $OUT_DIR/run.json"
echo "  - Ground truth: $TRUTH_FILE"
echo "  - Collector events: $EVENTS_FILE"
echo "  - Dropped events: $DROPS_FILE"

# 8. Restore normal user ownership so analysis scripts can read/write without sudo
if [ -n "$SUDO_UID" ] && [ -n "$SUDO_GID" ]; then
    chown -R "$SUDO_UID:$SUDO_GID" "$OUT_DIR"
fi
