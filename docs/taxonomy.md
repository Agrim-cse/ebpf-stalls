# Stall Taxonomy & Telemetry Ambiguity Formulation

## 1. Overview and Core Hypotheses
Accurately attributing latency stalls in modern multicore systems requires cross-layer observability. A thread observing high wall-clock latency during a system call or execution phase may be blocked on physical storage, waiting in a scheduler runqueue due to CPU oversubscription, or executing expensive computational logic.

This project investigates two core hypotheses:
- **H1 (Multi-Layer Fusion)**: Fusing syscall, scheduler, and block-layer telemetry achieves significantly higher classification fidelity than any isolated single-layer probe.
- **H2 (Abstention Under Ambiguity)**: Providing an explicit **Abstain / Indeterminate class** (`INDETERMINATE`) dramatically reduces high-confidence misclassifications when cross-layer signals are conflicting or poorly correlated.

---

## 2. Formal Stall Taxonomy

Each application operation window $[t_{\text{start}}, t_{\text{end}}]$ of duration $T = t_{\text{end}} - t_{\text{start}}$ is partitioned into canonical time components:
$$T = t_{\text{run}} + t_{\text{runnable}} + t_{\text{blocked}}$$

Where:
- $t_{\text{run}}$: Time actively executing on a CPU core (`TASK_RUNNING` on CPU).
- $t_{\text{runnable}}$: Time runnable on the CFS runqueue awaiting an available CPU core (`TASK_RUNNING` off CPU).
- $t_{\text{blocked}}$: Time descheduled off-CPU in sleep states ($t_{\text{blocked}} = t_{\text{uninterruptible}} + t_{\text{interruptible}}$).

```
                      +-----------------------------+
                      |     Operation Window T      |
                      +-----------------------------+
                                     |
         +---------------------------+---------------------------+
         |                           |                           |
         v                           v                           v
  +--------------+            +--------------+            +--------------+
  |    t_run     |            |  t_runnable  |            |  t_blocked   |
  |  (On-CPU)    |            |  (Runqueue)  |            |  (Off-CPU)   |
  +--------------+            +--------------+            +--------------+
         |                           |                           |
         |                           |             +-------------+-------------+
         |                           |             |                           |
         v                           v             v                           v
  [CPU_SYSCALL]                [SCHED_WAIT]  [t_uninterruptible]        [t_interruptible]
  (or WARM_READ                                (D-State / I/O)           (Locks/Timers)
   if duration < 5ms)                              |
                                                   v
                                               [IO_WAIT]
                                         (if block_issues > 0)
```

### 2.1 Target Classes

#### 1. `IO_WAIT` (Storage-Induced Stall)
- **Kernel Definition**: The thread enters `TASK_UNINTERRUPTIBLE` (`D` state, `prev_state = 2`) due to synchronous physical storage I/O.
- **Telemetry Signature**:
  - Uninterruptible sleep fraction $\frac{t_{\text{uninterruptible}}}{T} \ge 0.45$.
  - Temporal overlap with active block layer requests (`layer = 3, type = 0` `block_rq_issue` and `type = 1` `block_rq_complete`) on the target device.
  - Syscalls typically include `read`, `pread64`, `write`, `pwrite64`, `fsync`, `fdatasync`, or synchronous page faults.

#### 2. `SCHED_WAIT` (Scheduler / Runqueue Contention)
- **Kernel Definition**: The thread is runnable (`TASK_RUNNING`) but deprived of CPU cycles because higher-priority or co-scheduled threads saturate the physical cores.
- **Telemetry Signature**:
  - Runqueue delay fraction $\frac{t_{\text{runnable}}}{T} \ge 0.45$.
  - Captured via `sched_switch` where `prev_state == 0` (`TASK_STATE_RUNNABLE`), or the elapsed interval between `sched_wakeup` (`layer = 2, type = 2`) and the subsequent `sched_switch_in` (`layer = 2, type = 1`).
  - No active block I/O requests attributable to the operation window.

#### 3. `CPU_SYSCALL` (Compute Saturation)
- **Kernel Definition**: The thread remains actively executing on-CPU without descheduling, performing in-memory compute, cryptographic routines, or memory copies.
- **Telemetry Signature**:
  - On-CPU fraction $\frac{t_{\text{run}}}{T} \ge 0.75$.
  - Context switch count is minimal ($n_{\text{ctx}} \approx 0$).
  - Physical block requests count is zero ($\text{block\_issues} = 0$).

#### 4. `WARM_READ` (Page-Cache Resident Hit)
- **Kernel Definition**: A read system call satisfied entirely from the Linux Page Cache without traversing down to the storage block layer. It represents normal fast cached execution rather than an abnormal stall or CPU-bound computation.
- **Telemetry Signature**:
  - Operation duration is low ($T \le 5.0\text{ ms}$).
  - Syscall entry/exit captured (`read` / `pread64`), returning valid positive byte counts.
  - Zero block requests issued or completed ($\text{block\_issues} = 0$).

---

## 3. The Abstention Formulation (`INDETERMINATE`)

In production environments, telemetry is frequently subject to ambiguous, asynchronous, or colliding signals. Forcing a hard choice among `IO_WAIT`, `SCHED_WAIT`, and `CPU_SYSCALL` under high uncertainty leads to catastrophic false positives.

### 3.1 Sources of Ambiguity

1. **Decoupled Asynchronous Writeback (`kworker` collisions)**:
   Buffered writes modify page cache memory instantly and return. Later, asynchronous kernel flusher threads (`kworker/u*:*`) flush dirty pages to disk. If another operation executes while background flushing occurs, block requests will be observed during the window even though the application thread was never blocked waiting for them.
2. **Readahead & I/O Merging**:
   The Linux elevator scheduler merges contiguous I/O requests. An application requesting 4 KB might be merged into an ongoing 128 KB readahead request initiated earlier, blurring the exact start boundary.
3. **Non-Block Uninterruptible Sleep**:
   A thread may enter `TASK_UNINTERRUPTIBLE` while waiting on in-kernel mutexes, memory reclamation (direct reclaim), or NFS/network sockets rather than local block devices. If no block events occur, classifying this as `IO_WAIT` is erroneous.
4. **Multi-Bottleneck Mixed Operations**:
   Complex operations may experience balanced 35% on-CPU execution, 30% scheduler delay, and 35% I/O wait. Declaring a single dominant culprit without high confidence misleads performance engineers.

### 3.2 Formal Abstention Decision Boundary

The classifier outputs a prediction vector $\mathbf{p} = [p_{\text{io}}, p_{\text{sched}}, p_{\text{cpu}}, p_{\text{warm}}]$ and an associated confidence score $c = \max(\mathbf{p}) \in [0, 1]$.

An explicit abstention decision is enforced:
$$\hat{y} = \begin{cases} 
\arg\max_k p_k & \text{if } c \ge \tau \text{ and consistency checks pass} \\
\text{INDETERMINATE} & \text{if } c < \tau \text{ or conflicting evidence flags triggered}
\end{cases}$$

Where the threshold $\tau$ (e.g., $0.60$) is tuned strictly on the tuning set (`data/tune/`) and frozen prior to final test evaluation (`data/final/`).

### 3.3 Conflict Invariants Leading to Immediate Abstention
- **Conflict 1 (Phantom I/O)**: `io_sleep_ratio > 0.40` but `block_issues == 0`. (Blocked off-CPU, but no block layer evidence).
- **Conflict 2 (Background Noise False Attribution)**: `block_issues > 0` but `on_cpu_ratio > 0.80`. (Storage activity present, but application thread never stalled).
- **Conflict 3 (Entropy / Tie)**: $\max_k(p_k) - \text{second\_max}_k(p_k) < 0.15$. (No single dominant bottleneck).

