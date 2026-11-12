#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
#include <fcntl.h>
#include <string.h>
#include <errno.h>

#define ALIGNMENT 4096
#define IO_SIZE (1024 * 1024) // 1 MB

int main(int argc, char **argv)
{
    const char *filepath = "/home/priyansu/ebpf_lab/scratch/test_io.dat";
    if (argc > 1) {
        filepath = argv[1];
    }

    // Allocate aligned memory for O_DIRECT
    void *buf = NULL;
    if (posix_memalign(&buf, ALIGNMENT, IO_SIZE) != 0) {
        perror("posix_memalign failed");
        return 1;
    }

    // Phase A: Write 1MB to ensure file exists and is populated
    int fd_write = open(filepath, O_CREAT | O_WRONLY | O_TRUNC | O_DIRECT, 0644);
    if (fd_write < 0) {
        // Fallback without O_DIRECT if filesystem restricts O_DIRECT create
        fd_write = open(filepath, O_CREAT | O_WRONLY | O_TRUNC, 0644);
        if (fd_write < 0) {
            fprintf(stderr, "Cannot open %s for write: %s\n", filepath, strerror(errno));
            free(buf);
            return 1;
        }
    }
    memset(buf, 0xAB, IO_SIZE);
    ssize_t written = write(fd_write, buf, IO_SIZE);
    if (written < 0) {
        perror("write failed");
        close(fd_write);
        free(buf);
        return 1;
    }
    fsync(fd_write);
    close(fd_write);

    // Short sleep to generate a sched_switch (interruptible sleep)
    usleep(50000); // 50 ms

    // Phase B: Cold read using O_DIRECT
    int fd_read = open(filepath, O_RDONLY | O_DIRECT);
    if (fd_read < 0) {
        fprintf(stderr, "Cannot open %s with O_DIRECT for read: %s\n", filepath, strerror(errno));
        free(buf);
        return 1;
    }

    // Evict page cache just in case
    posix_fadvise(fd_read, 0, IO_SIZE, POSIX_FADV_DONTNEED);

    ssize_t n = read(fd_read, buf, IO_SIZE);
    if (n < 0) {
        perror("O_DIRECT read failed");
        close(fd_read);
        free(buf);
        return 1;
    }

    close(fd_read);
    free(buf);
    printf("Successfully executed cold O_DIRECT read of %zd bytes from %s\n", n, filepath);
    return 0;
}

