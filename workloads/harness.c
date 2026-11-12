#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <time.h>
#include <pthread.h>
#include <sched.h>
#include <sys/types.h>
#include <sys/syscall.h>

static uint64_t get_time_ns(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ULL + ts.tv_nsec;
}

static pid_t get_tid(void) {
    return (pid_t)syscall(SYS_gettid);
}

static const char *get_run_id(void) {
    const char *r = getenv("RUN_ID");
    return r ? r : "default_run";
}

/* 1. CPU Stall: Pure arithmetic spin loop */
void run_cpu_stall(int op_id, FILE *truth_fp, uint64_t duration_ms) {
    pid_t tid = get_tid();
    uint64_t start = get_time_ns();
    uint64_t target_end = start + (duration_ms * 1000000ULL);

    volatile uint64_t counter = 0;
    while (get_time_ns() < target_end) {
        counter++;
    }
    uint64_t end = get_time_ns();

    fprintf(truth_fp,
            "{\"run_id\":\"%s\",\"op_id\":%d,\"tid\":%d,\"start_ns\":%llu,\"end_ns\":%llu,\"syscall\":\"none\",\"bytes\":0,\"true_label\":\"CPU_SYSCALL\",\"label\":\"CPU_SYSCALL\"}\n",
            get_run_id(), op_id, tid, (unsigned long long)start, (unsigned long long)end);
    fflush(truth_fp);
}

/* 2. Cold Synchronous I/O: O_DIRECT read from disk scratch file */
void run_io_wait(int op_id, FILE *truth_fp, const char *filepath, size_t block_size) {
    pid_t tid = get_tid();
    
    // O_DIRECT requires aligned memory buffer
    void *buf = NULL;
    if (posix_memalign(&buf, 4096, block_size) != 0) {
        perror("posix_memalign");
        return;
    }

    int fd = open(filepath, O_RDONLY | O_DIRECT);
    if (fd < 0) {
        perror("open O_DIRECT");
        free(buf);
        return;
    }

    uint64_t start = get_time_ns();
    ssize_t bytes_read = read(fd, buf, block_size);
    uint64_t end = get_time_ns();

    close(fd);
    free(buf);

    if (bytes_read > 0) {
        fprintf(truth_fp,
                "{\"run_id\":\"%s\",\"op_id\":%d,\"tid\":%d,\"start_ns\":%llu,\"end_ns\":%llu,\"syscall\":\"read\",\"bytes\":%zd,\"true_label\":\"IO_WAIT\",\"label\":\"IO_WAIT\"}\n",
                get_run_id(), op_id, tid, (unsigned long long)start, (unsigned long long)end, bytes_read);
        fflush(truth_fp);
    }
}

/* 3. Warm Cached I/O: Buffered read from page-cache resident file */
void run_warm_read(int op_id, FILE *truth_fp, const char *filepath, size_t block_size) {
    pid_t tid = get_tid();
    char *buf = malloc(block_size);

    int fd = open(filepath, O_RDONLY);
    if (fd < 0) {
        perror("open warm file");
        free(buf);
        return;
    }

    uint64_t start = get_time_ns();
    ssize_t bytes_read = read(fd, buf, block_size);
    uint64_t end = get_time_ns();

    close(fd);
    free(buf);

    if (bytes_read > 0) {
        fprintf(truth_fp,
                "{\"run_id\":\"%s\",\"op_id\":%d,\"tid\":%d,\"start_ns\":%llu,\"end_ns\":%llu,\"syscall\":\"read\",\"bytes\":%zd,\"true_label\":\"WARM_READ\",\"label\":\"WARM_READ\"}\n",
                get_run_id(), op_id, tid, (unsigned long long)start, (unsigned long long)end, bytes_read);
        fflush(truth_fp);
    }
}

/* 4. Scheduler Contention: Pinned threads competing on single core */
struct worker_arg {
    int core_id;
    int duration_ms;
    volatile int *stop;
};

void *spinner_thread(void *arg) {
    struct worker_arg *w = (struct worker_arg *)arg;
    cpu_set_t cpuset;
    CPU_ZERO(&cpuset);
    CPU_SET(w->core_id, &cpuset);
    pthread_setaffinity_np(pthread_self(), sizeof(cpu_set_t), &cpuset);

    while (!(*w->stop)) {
        __asm__ __volatile__("" : : : "memory");
    }
    return NULL;
}

void run_sched_contend(int op_id, FILE *truth_fp, int num_competitors) {
    pid_t tid = get_tid();
    int target_core = 0;

    // Pin main thread to core 0
    cpu_set_t cpuset;
    CPU_ZERO(&cpuset);
    CPU_SET(target_core, &cpuset);
    sched_setaffinity(0, sizeof(cpu_set_t), &cpuset);

    volatile int stop_workers = 0;
    pthread_t *threads = malloc(sizeof(pthread_t) * num_competitors);
    struct worker_arg warg = { .core_id = target_core, .duration_ms = 0, .stop = &stop_workers };

    for (int i = 0; i < num_competitors; i++) {
        pthread_create(&threads[i], NULL, spinner_thread, &warg);
    }

    // Give background spinners a brief moment to start hogging the core
    usleep(1000);

    uint64_t start = get_time_ns();
    // Yield repeatedly while competing with pinned background spinners
    for (int y = 0; y < 100; y++) {
        sched_yield();
        volatile uint64_t spin = 0;
        for (int k = 0; k < 10000; k++) {
            spin += k;
        }
    }
    uint64_t end = get_time_ns();

    stop_workers = 1;
    for (int i = 0; i < num_competitors; i++) {
        pthread_join(threads[i], NULL);
    }
    free(threads);

    fprintf(truth_fp,
            "{\"run_id\":\"%s\",\"op_id\":%d,\"tid\":%d,\"start_ns\":%llu,\"end_ns\":%llu,\"syscall\":\"sched_yield\",\"bytes\":0,\"true_label\":\"SCHED_WAIT\",\"label\":\"SCHED_WAIT\"}\n",
            get_run_id(), op_id, tid, (unsigned long long)start, (unsigned long long)end);
    fflush(truth_fp);
}

void run_mixed_contend(int op_id, FILE *truth_fp, const char *filepath, size_t block_size, int num_competitors) {
    pid_t tid = get_tid();
    int target_core = 0;
    cpu_set_t cpuset;
    CPU_ZERO(&cpuset);
    CPU_SET(target_core, &cpuset);
    sched_setaffinity(0, sizeof(cpu_set_t), &cpuset);

    volatile int stop_workers = 0;
    pthread_t *threads = malloc(sizeof(pthread_t) * num_competitors);
    struct worker_arg warg = { .core_id = target_core, .duration_ms = 0, .stop = &stop_workers };
    for (int i = 0; i < num_competitors; i++) {
        pthread_create(&threads[i], NULL, spinner_thread, &warg);
    }
    usleep(1000);

    void *buf = NULL;
    if (posix_memalign(&buf, 4096, block_size) != 0) {
        stop_workers = 1;
        for (int i = 0; i < num_competitors; i++) pthread_join(threads[i], NULL);
        free(threads);
        return;
    }

    int fd = open(filepath, O_RDONLY | O_DIRECT);
    if (fd < 0) {
        free(buf);
        stop_workers = 1;
        for (int i = 0; i < num_competitors; i++) pthread_join(threads[i], NULL);
        free(threads);
        return;
    }

    uint64_t start = get_time_ns();
    ssize_t bytes_read = read(fd, buf, block_size);
    for (int y = 0; y < 50; y++) {
        sched_yield();
        volatile uint64_t spin = 0;
        for (int k = 0; k < 5000; k++) spin += k;
    }
    uint64_t end = get_time_ns();

    close(fd);
    free(buf);
    stop_workers = 1;
    for (int i = 0; i < num_competitors; i++) pthread_join(threads[i], NULL);
    free(threads);

    fprintf(truth_fp,
            "{\"run_id\":\"%s\",\"op_id\":%d,\"tid\":%d,\"start_ns\":%llu,\"end_ns\":%llu,\"syscall\":\"read\",\"bytes\":%zd,\"true_label\":\"INDETERMINATE\",\"label\":\"INDETERMINATE\"}\n",
            get_run_id(), op_id, tid, (unsigned long long)start, (unsigned long long)end, bytes_read > 0 ? bytes_read : 0);
    fflush(truth_fp);
}

int main(int argc, char **argv) {
    if (argc < 4) {
        fprintf(stderr, "Usage: %s <mode> <truth_file> <target_file_or_param> [block_size] [iterations]\n", argv[0]);
        fprintf(stderr, "Modes: cpu | io | warm | sched | mixed\n");
        return 1;
    }

    char *mode = argv[1];
    char *truth_path = argv[2];
    char *param = argv[3];
    size_t bs = (argc >= 5) ? atol(argv[4]) : 1048576; // default 1MB
    int iters = (argc >= 6) ? atoi(argv[5]) : 1;
    if (iters < 1) iters = 1;

    FILE *fp = fopen(truth_path, "a");
    if (!fp) {
        perror("fopen truth_path");
        return 1;
    }

    for (int i = 1; i <= iters; i++) {
        if (strcmp(mode, "cpu") == 0) {
            int duration = atoi(param);
            run_cpu_stall(i, fp, duration);
        } else if (strcmp(mode, "io") == 0) {
            run_io_wait(i, fp, param, bs);
        } else if (strcmp(mode, "warm") == 0) {
            run_warm_read(i, fp, param, bs);
        } else if (strcmp(mode, "sched") == 0) {
            int competitors = atoi(param);
            run_sched_contend(i, fp, competitors);
        } else if (strcmp(mode, "mixed") == 0) {
            run_mixed_contend(i, fp, param, bs, 4);
        } else {
            fprintf(stderr, "Unknown mode: %s\n", mode);
            break;
        }
        if (i < iters) {
            usleep(10000); // 10ms gap between operations
        }
    }

    fclose(fp);
    return 0;
}
