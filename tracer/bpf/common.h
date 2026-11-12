#pragma once

#ifndef __VMLINUX_H__
#include <linux/types.h>
#endif

// Layer identifiers
#define LAYER_SYSCALL 1
#define LAYER_SCHED   2
#define LAYER_BLOCK   3
#define LAYER_IRQ     4

// Syscall event types
#define SYS_ENTER     0
#define SYS_EXIT      1

// Sched event types
#define SCHED_SWITCH_OUT 0
#define SCHED_SWITCH_IN  1
#define SCHED_WAKEUP     2

// Sched task states on switch_out (normalized)
#define TASK_STATE_RUNNABLE       0
#define TASK_STATE_INTERRUPTIBLE   1
#define TASK_STATE_UNINTERRUPTIBLE 2

// Block event types
#define BLOCK_RQ_ISSUE    0
#define BLOCK_RQ_COMPLETE 1
#define BLOCK_BIO_QUEUE   2

// IRQ / Softirq event types
#define SOFTIRQ_ENTRY 0
#define SOFTIRQ_EXIT  1

struct bio_key {
    __u32 dev;
    __u64 sector;
};

struct event_t {
    __u64 ts_ns;
    __u32 pid;        // tgid
    __u32 tid;        // thread ID
    __u16 layer;      // 1 syscall, 2 sched, 3 block, 4 irq
    __u16 type;       // layer-specific type
    __u32 syscall_nr; // syscall ID (layer 1) or softirq vec (layer 4)
    __s64 ret;        // syscall return value, block error code, or softirq duration_ns
    __s64 prev_state; // sched switch_out: task state (0 runnable, 1 interruptible, 2 uninterruptible)
    __u64 sector;     // block starting sector
    __u32 dev;        // block device major:minor dev_t
    __u32 nr_bytes;   // block I/O byte count
    __u32 cpu;        // SMP processor ID
    __u32 target_tid; // sched wakeup: waker TID; block: submitter TID
    char  rwbs[8];    // block op flags (e.g., "R", "W", "RA")
};
