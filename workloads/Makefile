CC ?= gcc
CFLAGS ?= -O2 -Wall -pthread

all: harness

harness: harness.c
	$(CC) $(CFLAGS) $< -o $@

clean:
	rm -f harness

.PHONY: all clean

