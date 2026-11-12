.RECIPEPREFIX = >
CLANG ?= clang
LIBBPF ?= $(firstword $(wildcard $(CURDIR)/../libbpf-install $(HOME)/libbpf-install))
INC := -I. -Ibpf -I$(LIBBPF)/include
all: collector tests/test_direct_io
vmlinux.h:
> bpftool btf dump file /sys/kernel/btf/vmlinux format c > $@
trace.bpf.o: bpf/trace.bpf.c vmlinux.h bpf/common.h
> $(CLANG) -g -O2 -target bpf -D__TARGET_ARCH_x86 $(INC) -c $< -o $@
trace.skel.h: trace.bpf.o
> bpftool gen skeleton $< > $@
collector: src/collector.c trace.skel.h bpf/common.h
> $(CC) -g -O2 -Wall $(INC) $< -o $@ $(LIBBPF)/lib64/libbpf.a -lelf -lz
tests/test_direct_io: tests/test_direct_io.c
> $(CC) -g -O2 -Wall $< -o $@
clean:
> rm -f collector trace.bpf.o trace.skel.h tests/test_direct_io
