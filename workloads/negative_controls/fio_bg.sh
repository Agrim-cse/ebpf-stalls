#!/usr/bin/env bash
# Runs unbuffered background I/O using fio on a separate file while your target app runs

if [ -n "$1" ]; then
    SCRATCH_DIR="$1"
elif [ -n "$SUDO_USER" ]; then
    USER_HOME="$(getent passwd "$SUDO_USER" | cut -d: -f6)"
    SCRATCH_DIR="$USER_HOME/ebpf_lab/scratch"
else
    SCRATCH_DIR="$HOME/ebpf_lab/scratch"
fi

mkdir -p "$SCRATCH_DIR"

fio --name=background_noise \
    --filename="${SCRATCH_DIR}/bg_noise.dat" \
    --size=512M \
    --rw=randwrite \
    --bs=64k \
    --direct=1 \
    --numjobs=2 \
    --time_based \
    --runtime=10 \
    --group_reporting &
FIO_PID=$!

echo $FIO_PID
