PY ?= .venv/bin/python
PIP ?= $(PY) -m pip

.PHONY: help venv install test lint fmt status policy doctor clean

help:
	@echo "make install   create .venv and install with dev extras"
	@echo "make test      run the test suite"
	@echo "make lint      ruff check"
	@echo "make fmt       ruff check --fix"
	@echo "make status    vito status"
	@echo "make policy    vito policy"
	@echo "make doctor    vito doctor"
	@echo "make clean     remove caches and build artefacts"

venv:
	python3 -m venv --without-pip .venv
	curl -sSL https://bootstrap.pypa.io/get-pip.py | .venv/bin/python

install:
	$(PIP) install -e ".[dev]"

test:
	$(PY) -m pytest

lint:
	.venv/bin/ruff check .

fmt:
	.venv/bin/ruff check . --fix

status:
	$(PY) -m vito status

policy:
	$(PY) -m vito policy

doctor:
	$(PY) -m vito doctor

clean:
	rm -rf .pytest_cache .ruff_cache build dist *.egg-info src/*.egg-info
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
