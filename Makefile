.PHONY: check

check:
	ruff check .
	pytest -q
	mypy --strict src
