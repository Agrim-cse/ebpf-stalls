# eBPF Tracing Architecture & Collector Design

## 1. Overview and Design Objectives
The tracing subsystem serves as the core measurement engine for fingerprinting I/O-induced application stalls. To disambiguate execution states (on-CPU computation vs. runqueue scheduling delays vs. storage block latency), the tracer captures three complementary kernel layers:
1. **Application / Syscall Boundary**: Captures synchronous boundary transitions (`sys_enter` and `sys_exit`), identifying system call identities, durations, arguments, and return codes.
2. **CPU Scheduler Layer**: Traces thread state changes, preemption, voluntary vs. involuntary descheduling, and wakeups (`sched_switch`, `sched_wakeup`), enabling precise breakdown between active execution (`TASK_RUNNING`), runqueue delay (runnable-not-running), and blocked states (`TASK_INTERRUPTIBLE` vs. `TASK_UNINTERRUPTIBLE`).
3. **Block I/O Layer**: Captures physical storage requests (`block_rq_issue`, `block_rq_complete`), exposing physical device IDs, starting sectors, byte lengths, and latency windows.

All events are unified across cores through a single non-blocking ring buffer and ordered via kernel monotonic time (`CLOCK_MONOTONIC`).

```
+-----------------------------------------------------------------------------------+
|                                Userspace Workload                                 |
+-----------------------------------------------------------------------------------+
       | (Syscall Enter)                                       ^ (Syscall Exit)
       v                                                       |
+-----------------------------------------------------------------------------------+
| Linux Kernel                                                                      |
|                                                                                   |
|  [tracepoint/raw_syscalls/sys_enter]         [tracepoint/raw_syscalls/sys_exit]   |
|         |                                                   |                     |
|         +-------------------+       +-----------------------+                     |
|                             v       v                                             |
|                     [tids Map] <--- (Dynamic Thread Registration)                 |
|                             |                                                     |
|  [tracepoint/sched/sched_switch]             [tracepoint/sched/sched_wakeup]      |
|  (prev_state: R=0, S=1, D=2)                 (target TID + waker TID)             |
|         |                                                   |                     |
|         +-------------------+       +-----------------------+                     |
|                             v       v                                             |
|  [tracepoint/block/block_rq_issue]           [tracepoint/block/block_rq_complete] |
|  (sector, dev, nr_bytes, rwbs)               (dev, nr_bytes, duration, error)     |
|         |                                                   |                     |
|         +------------------------+--------------------------+                     |
|                                  |                                                |
|                                  v                                                |
|                     [BPF Ring Buffer: 64 MB]                                      |
|                     [Atomic Drops Counter]                                        |
+-----------------------------------------------------------------------------------+
                                   |
                                   | ring_buffer__poll() / ring_buffer__consume()
                                   v
+-----------------------------------------------------------------------------------+
| Userspace Collector (`collector.c`)                                               |
| - Synchronous child fork/exec handshake (pipe-based barrier)                      |
| - Emits unified `events.csv` (14 columns)                                         |
| - Records exact per-trial drop counts in `drops.txt`                              |
+-----------------------------------------------------------------------------------+
```

---

## 2. In-Kernel Probe Placement & Data Structures

### 2.1 Extended Event Schema
To minimize ring buffer memory footprint while avoiding variable-length encoding penalties, all events share a single fixed-size 72-byte struct `struct event_t`:

```c
struct event_t {
    __u64 ts_ns;        // CLOCK_MONOTONIC timestamp
    __u32 pid;          // TGID (Process ID)
    __u32 tid;          // Thread ID
    __u16 layer;        // 1: Syscall, 2: Sched, 3: Block
    __u16 type;         // Layer-specific event type
    __u32 syscall_nr;   // Syscall ID (x86_64 unistd)
    __s64 ret;          // Return code / block error
    __s64 prev_state;   // Normalized task state on deschedule (0=R, 1=S, 2=D)
    __u64 sector;       // Block starting sector
    __u32 dev;          // Block device major:minor
    __u32 nr_bytes;     // Byte length of I/O
    __u32 cpu;          // SMP CPU core index
    __u32 target_tid;   // Waker TID (sched_wakeup)
    char  rwbs[8];      // Block operation flags (R, W, RA, etc.)
};
```

### 2.2 Dynamic Thread Tracking (`tids` Map)
A fundamental challenge in Linux scheduler tracing is that tracepoint arguments in `sched_switch` (`prev_pid`, `next_pid`) and `sched_wakeup` (`pid`) supply kernel thread IDs (`pid_t`), which correspond to POSIX thread IDs (`tid`), not the userspace thread group leader (`tgid`).

Furthermore, in tracepoint context, reading the `task_struct` of arbitrary remote tasks requires dereferencing kernel pointers that may vary across kernel revisions. To resolve this cleanly within verifier constraints:
1. The collector registers the primary process TGID into a BPF hash map `tracked` (`tgid -> 1`) and seeds `tids` (`tgid -> tgid`).
2. On every `sys_enter` invocation by a tracked process, the tracer dynamically maps `tid -> tgid` in the BPF hash map `tids`.
3. In `sched_switch`, the tracer inspects `prev_pid` and `next_pid` against `tids`. If either thread belongs to the tracked workload, an event is emitted.
4. In `sched_wakeup`, the target thread `ctx->pid` is checked against `tids`. The waker thread is attributed via `bpf_get_current_pid_tgid()`.

### 2.3 Task State Normalization
When a task leaves the CPU, `ctx->prev_state` in modern Linux kernels encodes composite bitmasks (e.g., `TASK_INTERRUPTIBLE (0x01)`, `TASK_UNINTERRUPTIBLE (0x02)`, `TASK_IDLE (0x0402)`). The tracer normalizes this into canonical tri-state values:
- **`0` (`TASK_STATE_RUNNABLE`)**: Preempted or yield. Task remains on the runqueue. Represents pure scheduler contention (`SCHED_WAIT`).
- **`1` (`TASK_STATE_INTERRUPTIBLE`)**: Voluntary deschedule awaiting synchronous synchronization, lock contention, or timers.
- **`2` (`TASK_STATE_UNINTERRUPTIBLE`)**: Involuntary wait, predominantly deep storage I/O stalls (`IO_WAIT`).

---

## 3. Userspace Collector & Synchronization Handshake

### 3.1 Workload Lifecycle Handshake
To prevent race conditions where short-lived operations execute before eBPF programs are fully attached, the collector establishes a synchronous pipeline barrier:
1. `collector` creates an internal synchronization pipe and calls `fork()`.
2. The child process closes the write end and performs a blocking single-byte `read()` on the pipe before `execvp()`.
3. The parent process opens, loads, and attaches the BPF skeleton via libbpf, populating the initial PID in `tracked` and `tids`.
4. Once tracepoints are armed and the ring buffer is online, the parent writes a token to the synchronization pipe, releasing the child to execute the workload command.
5. If the `--ready-file <path>` flag is specified, the collector touches the ready flag file, signaling external workload orchestrators (T1).

### 3.2 Loss Detection & Zero-Copy Ring Buffer
To avoid user-kernel copy overhead, events are submitted to a 64 MB BPF ring buffer (`BPF_MAP_TYPE_RINGBUF`). Ring buffer slots are reserved via `bpf_ringbuf_reserve()`. If memory pressure causes reservation failure, an atomic counter in an array map (`drops`) is incremented.

Upon process termination, `ring_buffer__consume()` drains all outstanding events, and the final drop count is persisted to `drops.txt`.

---

## 4. Kernel Compatibility & Verifier Considerations

### 4.1 BPF CO-RE & Static libbpf 1.4 Linking
On Ubuntu 22.04 LTS with HWE kernel `6.8.0-138-generic`, the distribution's packaged `libbpf 0.5.x` fails to parse newer kernel BTF encodings (`"failed to find valid kernel BTF"`). To guarantee portability and avoid verifier rejections:
- `libbpf v1.4.0` was built from source and statically linked.
- The tracer compiles against `vmlinux.h` generated from `/sys/kernel/btf/vmlinux`, ensuring Compile Once - Run Everywhere (CO-RE) field relocations.
- `bpf_probe_read_kernel_str` and direct tracepoint context dereferences comply with all kernel 6.8 verifier safety checks.

### 4.2 Tracepoint Stability
- `raw_syscalls/sys_enter` and `raw_syscalls/sys_exit`: Stable across all Linux 4.x/5.x/6.x kernels.
- `sched/sched_switch` and `sched/sched_wakeup`: Field definitions have remained backward-compatible; task state bitmasks are robustly decoded via bitwise tests (`& 2`, `& 1`).
- `block/block_rq_issue` and `block/block_rq_complete`: In Linux 6.8, block completion tracepoints expose `nr_sector` rather than raw bytes; the tracer translates `nr_sector << 9` to maintain strict byte accounting parity.

---

## 5. Inherent Observability Boundaries & Threats to Validity

1. **Decoupled Block Attribution (kworkers & softirq)**:
   In asynchronous buffered I/O, dirty pages are flushed by kernel flusher threads (`kworker`), not the originating process. Similarly, block request completion interrupts are processed in softirq context. Consequently, block events cannot be filtered by PID alone. As designed, block events are captured system-wide, and T3 correlates them via storage device `dev`, sector contiguousness, and temporal overlap windows.
2. **Page Cache & Read-Ahead Shielding**:
   Buffered reads satisfied entirely from page cache do not trigger block-layer tracepoints. This behavior is deliberate: cached reads manifest as low-latency `CPU_SYSCALL` events, contrasting sharply with cold synchronous reads (`IO_WAIT`).
3. **Softirq CPU Accounting**:
   When disk interrupts occur on the same CPU running an application, the CPU time charged to the application may include interrupt handling overhead. While this overhead is minimal under NVMe, it is explicitly cataloged as a known threat to validity for high-throughput baseline evaluations.

