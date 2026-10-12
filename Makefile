.PHONY: v2-gate

PHASE ?= 0
ARGS ?=

v2-gate:
	./scripts/v2_gate.sh $(PHASE) $(ARGS)

