#!/bin/sh
set -eu

ruff check .
pytest -q
mypy --strict src
mypy --strict harness
