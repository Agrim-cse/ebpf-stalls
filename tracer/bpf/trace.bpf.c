#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include "common.h"

struct {
    __uint(type, BPF_MAP_TYPE_RINGBUF);
    __uint(max_entries, 1 << 26); // 64 MB
} rb SEC(".maps");

// Map of tracked thread group IDs (tgid -> 1)
struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 1024);
    __type(key, __u32);
    __type(value, __u8);
} tracked SEC(".maps");

// Map of tracked thread IDs to their tgid (tid -> tgid)
struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 8192);
    __type(key, __u32);
    __type(value, __u32);
} tids SEC(".maps");

// Counter for dropped events due to ring buffer overflow
struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, __u64);
} drops SEC(".maps");

// Map correlating block bio submissions to submitting thread TID: (dev, sector) -> tid
struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 16384);
    __type(key, struct bio_key);
    __type(value, __u32);
} bio_submitters SEC(".maps");

struct softirq_entry_t {
    __u64 start_ts;
    __u32 vec;
    __u32 tid;
    __u32 tgid;
};

// Per-CPU tracker for active softirq interruptions
struct {
    __uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, struct softirq_entry_t);
} cur_softirq SEC(".maps");

static __always_inline struct event_t *reserve_event(__u16 layer, __u16 type)
{
    struct event_t *e = bpf_ringbuf_reserve(&rb, sizeof(*e), 0);
    if (!e) {
        __u32 k = 0;
        __u64 *d = bpf_map_lookup_elem(&drops, &k);
        if (d) {
            __sync_fetch_and_add(d, 1);
        }
        return 0;
    }
    __builtin_memset(e, 0, sizeof(*e));
    e->ts_ns = bpf_ktime_get_ns();
    e->layer = layer;
    e->type = type;
    e->cpu = bpf_get_smp_processor_id();
    return e;
}

// -----------------------------------------------------------------------------
// Layer 1: Syscalls
// -----------------------------------------------------------------------------

SEC("tracepoint/raw_syscalls/sys_enter")
int on_enter(struct trace_event_raw_sys_enter *ctx)
{
    __u64 pid_tgid = bpf_get_current_pid_tgid();
    __u32 tgid = pid_tgid >> 32;
    __u32 tid = (__u32)pid_tgid;

    if (!bpf_map_lookup_elem(&tracked, &tgid))
        return 0;

    // Dynamically register TID under this TGID for scheduler correlation
    bpf_map_update_elem(&tids, &tid, &tgid, BPF_ANY);

    struct event_t *e = reserve_event(LAYER_SYSCALL, SYS_ENTER);
    if (!e)
        return 0;

    e->pid = tgid;
    e->tid = tid;
    e->syscall_nr = ctx->id;
    bpf_ringbuf_submit(e, 0);
    return 0;
}

SEC("tracepoint/raw_syscalls/sys_exit")
int on_exit(struct trace_event_raw_sys_exit *ctx)
{
    __u64 pid_tgid = bpf_get_current_pid_tgid();
    __u32 tgid = pid_tgid >> 32;
    __u32 tid = (__u32)pid_tgid;

    if (!bpf_map_lookup_elem(&tracked, &tgid))
        return 0;

    struct event_t *e = reserve_event(LAYER_SYSCALL, SYS_EXIT);
    if (!e)
        return 0;

    e->pid = tgid;
    e->tid = tid;
    e->syscall_nr = ctx->id;
    e->ret = ctx->ret;
    bpf_ringbuf_submit(e, 0);
    return 0;
}

// -----------------------------------------------------------------------------
// Layer 2: Scheduler
// -----------------------------------------------------------------------------

SEC("tracepoint/sched/sched_switch")
int on_sched_switch(struct trace_event_raw_sched_switch *ctx)
{
    __u32 prev_tid = ctx->prev_pid;
    __u32 next_tid = ctx->next_pid;

    // Check if prev task (switching out) is tracked
    __u32 *prev_tgid = bpf_map_lookup_elem(&tids, &prev_tid);
    if (prev_tgid) {
        struct event_t *e = reserve_event(LAYER_SCHED, SCHED_SWITCH_OUT);
        if (e) {
            e->pid = *prev_tgid;
            e->tid = prev_tid;
            // Normalize task state:
            // 0: Runnable/preempted (TASK_RUNNING == 0)
            // 1: Interruptible sleep (bit 0x0001)
            // 2: Uninterruptible sleep / I/O wait (bit 0x0002)
            long state = ctx->prev_state;
            if (state & 2)
                e->prev_state = TASK_STATE_UNINTERRUPTIBLE;
            else if (state & 1)
                e->prev_state = TASK_STATE_INTERRUPTIBLE;
            else
                e->prev_state = TASK_STATE_RUNNABLE;

            bpf_ringbuf_submit(e, 0);
        }
    }

    // Check if next task (switching in) is tracked
    __u32 *next_tgid = bpf_map_lookup_elem(&tids, &next_tid);
    if (next_tgid) {
        struct event_t *e = reserve_event(LAYER_SCHED, SCHED_SWITCH_IN);
        if (e) {
            e->pid = *next_tgid;
            e->tid = next_tid;
            bpf_ringbuf_submit(e, 0);
        }
    }

    return 0;
}

SEC("tracepoint/sched/sched_wakeup")
int on_sched_wakeup(struct trace_event_raw_sched_wakeup_template *ctx)
{
    __u32 target_tid = ctx->pid;

    // Check if the thread being woken is tracked
    __u32 *tgid = bpf_map_lookup_elem(&tids, &target_tid);
    if (!tgid)
        return 0;

    struct event_t *e = reserve_event(LAYER_SCHED, SCHED_WAKEUP);
    if (!e)
        return 0;

    e->pid = *tgid;
    e->tid = target_tid;
    e->target_tid = (__u32)bpf_get_current_pid_tgid(); // Waker TID
    bpf_ringbuf_submit(e, 0);
    return 0;
}

// -----------------------------------------------------------------------------
// Layer 3: Block I/O
// -----------------------------------------------------------------------------

SEC("tracepoint/block/block_bio_queue")
int on_block_bio_queue(struct trace_event_raw_block_bio *ctx)
{
    __u64 pid_tgid = bpf_get_current_pid_tgid();
    __u32 tgid = pid_tgid >> 32;
    __u32 tid = (__u32)pid_tgid;

    if (!bpf_map_lookup_elem(&tracked, &tgid) && !bpf_map_lookup_elem(&tids, &tid))
        return 0;

    struct bio_key key = {
        .dev = ctx->dev,
        .sector = ctx->sector,
    };
    bpf_map_update_elem(&bio_submitters, &key, &tid, BPF_ANY);

    struct event_t *e = reserve_event(LAYER_BLOCK, BLOCK_BIO_QUEUE);
    if (e) {
        e->pid = tgid;
        e->tid = tid;
        e->dev = ctx->dev;
        e->sector = ctx->sector;
        e->nr_bytes = ctx->nr_sector << 9;
        e->target_tid = tid;
        __builtin_memcpy(e->rwbs, ctx->rwbs, sizeof(e->rwbs));
        bpf_ringbuf_submit(e, 0);
    }
    return 0;
}

SEC("tracepoint/block/block_rq_issue")
int on_block_rq_issue(struct trace_event_raw_block_rq *ctx)
{
    struct event_t *e = reserve_event(LAYER_BLOCK, BLOCK_RQ_ISSUE);
    if (!e)
        return 0;

    __u64 pid_tgid = bpf_get_current_pid_tgid();
    e->pid = pid_tgid >> 32;
    e->tid = (__u32)pid_tgid;
    e->dev = ctx->dev;
    e->sector = ctx->sector;
    e->nr_bytes = ctx->bytes;
    __builtin_memcpy(e->rwbs, ctx->rwbs, sizeof(e->rwbs));

    struct bio_key key = {
        .dev = ctx->dev,
        .sector = ctx->sector,
    };
    __u32 *sub_tid = bpf_map_lookup_elem(&bio_submitters, &key);
    if (sub_tid) {
        e->target_tid = *sub_tid;
    }

    bpf_ringbuf_submit(e, 0);
    return 0;
}

SEC("tracepoint/block/block_rq_complete")
int on_block_rq_complete(struct trace_event_raw_block_rq_completion *ctx)
{
    struct event_t *e = reserve_event(LAYER_BLOCK, BLOCK_RQ_COMPLETE);
    if (!e)
        return 0;

    __u64 pid_tgid = bpf_get_current_pid_tgid();
    e->pid = pid_tgid >> 32;
    e->tid = (__u32)pid_tgid;
    e->dev = ctx->dev;
    e->sector = ctx->sector;
    e->nr_bytes = ctx->nr_sector << 9;
    e->ret = ctx->error;
    __builtin_memcpy(e->rwbs, ctx->rwbs, sizeof(e->rwbs));

    struct bio_key key = {
        .dev = ctx->dev,
        .sector = ctx->sector,
    };
    __u32 *sub_tid = bpf_map_lookup_elem(&bio_submitters, &key);
    if (sub_tid) {
        e->target_tid = *sub_tid;
        bpf_map_delete_elem(&bio_submitters, &key);
    }

    bpf_ringbuf_submit(e, 0);
    return 0;
}

// -----------------------------------------------------------------------------
// Layer 4: IRQ / SoftIRQ
// -----------------------------------------------------------------------------

SEC("tracepoint/irq/softirq_entry")
int on_softirq_entry(struct trace_event_raw_softirq *ctx)
{
    __u64 pid_tgid = bpf_get_current_pid_tgid();
    __u32 tgid = pid_tgid >> 32;
    __u32 tid = (__u32)pid_tgid;

    if (!bpf_map_lookup_elem(&tids, &tid) && !bpf_map_lookup_elem(&tracked, &tgid))
        return 0;

    __u32 zero = 0;
    struct softirq_entry_t *entry = bpf_map_lookup_elem(&cur_softirq, &zero);
    if (entry) {
        entry->start_ts = bpf_ktime_get_ns();
        entry->vec = ctx->vec;
        entry->tid = tid;
        entry->tgid = tgid;
    }
    return 0;
}

SEC("tracepoint/irq/softirq_exit")
int on_softirq_exit(struct trace_event_raw_softirq *ctx)
{
    __u32 zero = 0;
    struct softirq_entry_t *entry = bpf_map_lookup_elem(&cur_softirq, &zero);
    if (!entry || entry->start_ts == 0)
        return 0;

    __u64 now = bpf_ktime_get_ns();
    __u64 dur_ns = now - entry->start_ts;
    entry->start_ts = 0;

    struct event_t *e = reserve_event(LAYER_IRQ, SOFTIRQ_EXIT);
    if (!e)
        return 0;

    e->ts_ns = now;
    e->pid = entry->tgid;
    e->tid = entry->tid;
    e->syscall_nr = ctx->vec;
    e->ret = dur_ns;
    bpf_ringbuf_submit(e, 0);
    return 0;
}

char LICENSE[] SEC("license") = "GPL";
