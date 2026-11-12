all:
	$(MAKE) -C tracer
	$(MAKE) -C workloads

clean:
	$(MAKE) -C tracer clean
	$(MAKE) -C workloads clean

.PHONY: all clean

