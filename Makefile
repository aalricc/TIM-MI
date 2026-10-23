.PHONY: check harness audit

check:
	ruff check .
	pytest -q
	mypy --strict src
	mypy --strict harness

harness:
	pytest -q tests/test_harness_contracts.py tests/test_harness_mappings.py \
		tests/test_harness_sensors.py tests/test_harness_kpis.py tests/test_harness_architecture.py
	mypy --strict harness

audit:
	pip-audit --skip-editable --progress-spinner off
