#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <errno.h>
#include <signal.h>
#include <getopt.h>
#include <sys/wait.h>
#include <sys/stat.h>
#include <linux/types.h>
#include <bpf/libbpf.h>
#include <bpf/bpf.h>
#include "common.h"
#include "trace.skel.h"

static volatile sig_atomic_t exiting = 0;
static FILE *out_fp = NULL;

static void sig_handler(int sig)
{
    exiting = 1;
}

static int handle_event(void *ctx, void *data, size_t sz)
{
    const struct event_t *e = data;
    if (sz < sizeof(struct event_t))
        return 0;

    // Sanitize rwbs string (ensure null-terminated)
    char rwbs_buf[9];
    rwbs_buf[8] = '\0';
    memcpy(rwbs_buf, e->rwbs, sizeof(e->rwbs));

    // CSV format:
    // ts_ns,pid,tid,layer,type,syscall_nr,ret,prev_state,sector,dev,nr_bytes,cpu,target_tid,rwbs
    fprintf(out_fp ? out_fp : stdout,
            "%llu,%u,%u,%u,%u,%u,%lld,%lld,%llu,%u,%u,%u,%u,%s\n",
            (unsigned long long)e->ts_ns,
            e->pid,
            e->tid,
            e->layer,
            e->type,
            e->syscall_nr,
            (long long)e->ret,
            (long long)e->prev_state,
            (unsigned long long)e->sector,
            e->dev,
            e->nr_bytes,
            e->cpu,
            e->target_tid,
            rwbs_buf);
    return 0;
}

static int check_tracepoint(const char *category, const char *name)
{
    char path[256];
    struct stat st;
    snprintf(path, sizeof(path), "/sys/kernel/tracing/events/%s/%s", category, name);
    if (stat(path, &st) == 0)
        return 1;
    snprintf(path, sizeof(path), "/sys/kernel/debug/tracing/events/%s/%s", category, name);
    if (stat(path, &st) == 0)
        return 1;
    return 0;
}

static int check_capabilities(void)
{
    if (geteuid() != 0) {
        fprintf(stderr, "WARNING: collector typically requires root privileges (CAP_BPF / CAP_SYS_ADMIN) to attach tracepoints.\n");
    }

    struct stat st;
    if (stat("/sys/kernel/tracing", &st) != 0 && stat("/sys/kernel/debug/tracing", &st) != 0) {
        fprintf(stderr, "WARNING: tracefs filesystem not found at /sys/kernel/tracing or /sys/kernel/debug/tracing.\n");
    }

    if (!check_tracepoint("block", "block_bio_queue")) {
        fprintf(stderr, "NOTE: tracepoint block/block_bio_queue not accessible (will proceed with block_rq_issue)\n");
    }
    if (!check_tracepoint("irq", "softirq_entry")) {
        fprintf(stderr, "NOTE: tracepoint irq/softirq_entry not accessible\n");
    }
    return 0;
}

static void print_usage(const char *prog)
{
    fprintf(stderr,
            "Usage: %s [OPTIONS] -- <cmd> [args...]\n"
            "   or: %s [OPTIONS] -p <pid>\n\n"
            "Options:\n"
            "  -r, --run-id <id>       Run identifier string (default: default_run)\n"
            "  -o, --out <file>        Output events CSV file path (default: stdout)\n"
            "  -d, --drops <file>      Drops report file path (default: drops.txt)\n"
            "  -p, --pid <tgid>        Track existing process TGID instead of spawning\n"
            "  -R, --ready-file <path> Touch file when tracer is attached and ready\n"
            "  -h, --help              Show this help message\n",
            prog, prog);
}

int main(int argc, char **argv)
{
    const char *run_id = "default_run";
    const char *out_path = NULL;
    const char *drops_path = "drops.txt";
    const char *ready_file = NULL;
    pid_t target_pid = 0;
    int cmd_idx = 0;

    static struct option long_options[] = {
        {"run-id",     required_argument, 0, 'r'},
        {"out",        required_argument, 0, 'o'},
        {"drops",      required_argument, 0, 'd'},
        {"pid",        required_argument, 0, 'p'},
        {"ready-file", required_argument, 0, 'R'},
        {"help",       no_argument,       0, 'h'},
        {0, 0, 0, 0}
    };

    int opt;
    while ((opt = getopt_long(argc, argv, "r:o:d:p:R:h", long_options, NULL)) != -1) {
        switch (opt) {
        case 'r': run_id = optarg; break;
        case 'o': out_path = optarg; break;
        case 'd': drops_path = optarg; break;
        case 'p': target_pid = (pid_t)atoi(optarg); break;
        case 'R': ready_file = optarg; break;
        case 'h': print_usage(argv[0]); return 0;
        default:
            print_usage(argv[0]);
            return 1;
        }
    }

    if (optind < argc) {
        if (strcmp(argv[optind], "--") == 0) {
            cmd_idx = optind + 1;
        } else {
            cmd_idx = optind;
        }
    }

    if (target_pid == 0 && cmd_idx >= argc) {
        fprintf(stderr, "Error: must specify either -- <cmd> [args...] or -p <pid>\n");
        print_usage(argv[0]);
        return 1;
    }

    check_capabilities();

    signal(SIGINT, sig_handler);
    signal(SIGTERM, sig_handler);

    if (out_path) {
        out_fp = fopen(out_path, "w");
        if (!out_fp) {
            fprintf(stderr, "Error opening output file '%s': %s\n", out_path, strerror(errno));
            return 1;
        }
    } else {
        out_fp = stdout;
    }

    // Set line buffering on output stream
    setvbuf(out_fp, NULL, _IOLBF, 0);

    // Setup child synchronization pipe for command mode
    int sync_pipe[2] = {-1, -1};
    pid_t child_pid = 0;

    if (cmd_idx < argc) {
        if (pipe(sync_pipe) != 0) {
            perror("pipe failed");
            return 1;
        }

        child_pid = fork();
        if (child_pid < 0) {
            perror("fork failed");
            return 1;
        }

        if (child_pid == 0) {
            // Child: wait for parent signal on sync_pipe before executing
            close(sync_pipe[1]);
            char token;
            if (read(sync_pipe[0], &token, 1) < 0) {
                perror("child sync read failed");
                _exit(127);
            }
            close(sync_pipe[0]);
            execvp(argv[cmd_idx], &argv[cmd_idx]);
            fprintf(stderr, "execvp '%s' failed: %s\n", argv[cmd_idx], strerror(errno));
            _exit(127);
        }

        close(sync_pipe[0]);
        target_pid = child_pid;
    }

    // Load and verify BPF skeleton
    struct trace_bpf *skel = trace_bpf__open_and_load();
    if (!skel) {
        fprintf(stderr, "Error: failed to open and load BPF skeleton\n");
        if (child_pid > 0) kill(child_pid, SIGKILL);
        return 1;
    }

    if (trace_bpf__attach(skel)) {
        fprintf(stderr, "Error: failed to attach BPF programs\n");
        trace_bpf__destroy(skel);
        if (child_pid > 0) kill(child_pid, SIGKILL);
        return 1;
    }

    // Register tracked target PID (TGID) and initial TID
    __u32 tgid_key = target_pid;
    __u8 val = 1;
    bpf_map_update_elem(bpf_map__fd(skel->maps.tracked), &tgid_key, &val, BPF_ANY);
    bpf_map_update_elem(bpf_map__fd(skel->maps.tids), &tgid_key, &tgid_key, BPF_ANY);

    // Create ring buffer
    struct ring_buffer *rb = ring_buffer__new(bpf_map__fd(skel->maps.rb), handle_event, NULL, NULL);
    if (!rb) {
        fprintf(stderr, "Error: failed to create ring buffer\n");
        trace_bpf__destroy(skel);
        if (child_pid > 0) kill(child_pid, SIGKILL);
        return 1;
    }

    // Write CSV header
    fprintf(out_fp, "ts_ns,pid,tid,layer,type,syscall_nr,ret,prev_state,sector,dev,nr_bytes,cpu,target_tid,rwbs\n");
    fflush(out_fp);

    // If ready file requested, touch it to signal harness
    if (ready_file) {
        FILE *rf = fopen(ready_file, "w");
        if (rf) {
            fprintf(rf, "ready\n");
            fclose(rf);
        }
    }

    // If we spawned child, release it now that tracer is armed
    if (child_pid > 0) {
        if (write(sync_pipe[1], "GO", 1) < 0) {
            perror("write to child sync pipe failed");
        }
        close(sync_pipe[1]);
    }

    fprintf(stderr, "[collector] tracking TGID %d (run_id: %s)\n", target_pid, run_id);

    // Event collection loop
    int status;
    while (!exiting) {
        ring_buffer__poll(rb, 100);

        if (child_pid > 0) {
            pid_t w = waitpid(child_pid, &status, WNOHANG);
            if (w == child_pid) {
                // Child has finished
                break;
            }
        } else if (target_pid > 0) {
            // In attach mode, verify target process is still alive
            if (kill(target_pid, 0) != 0 && errno == ESRCH) {
                break;
            }
        }
    }

    // Flush any pending events remaining in ring buffer
    ring_buffer__consume(rb);

    // Query dropped events
    __u32 drop_key = 0;
    __u64 dropped = 0;
    bpf_map_lookup_elem(bpf_map__fd(skel->maps.drops), &drop_key, &dropped);

    fprintf(stderr, "[collector] run_id %s finished. dropped events: %llu\n", run_id, (unsigned long long)dropped);

    // Write drops report
    if (drops_path) {
        FILE *df = fopen(drops_path, "w");
        if (df) {
            fprintf(df, "%llu\n", (unsigned long long)dropped);
            fclose(df);
        } else {
            fprintf(stderr, "Failed to write drops file '%s': %s\n", drops_path, strerror(errno));
        }
    }

    if (out_fp && out_fp != stdout) {
        fclose(out_fp);
    }

    ring_buffer__free(rb);
    trace_bpf__destroy(skel);
    return 0;
}
