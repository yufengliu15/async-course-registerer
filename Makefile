PYTHON ?= .venv/bin/python

.PHONY: check format lint test

check: lint test
	$(PYTHON) -m ruff format --check .
	$(PYTHON) -m compileall -q carleton_watch.py tests

format:
	$(PYTHON) -m ruff format .

lint:
	$(PYTHON) -m ruff check .

test:
	$(PYTHON) -m pytest
