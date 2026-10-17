# ANTIBODY repository commands. Each target is a thin wrapper over a documented script,
# so the same commands work without make:  python scripts/setup.py, python -m server.cli ...
ifeq ($(OS),Windows_NT)
PY := .venv/Scripts/python.exe
else
PY := .venv/bin/python
endif

.PHONY: setup dev test test-security eval demo demo-reset export lock smoke gc backup

setup:          ## install pinned dependencies, build the UI, migrate
	python scripts/setup.py

dev:            ## start API + worker pool and the web client (loopback only)
	$(PY) scripts/dev.py

test:           ## deterministic unit and integration tests (T01-T14)
	$(PY) -m pytest -q

test-security:  ## gateway, source and restore regressions
	$(PY) -m pytest -q -m security

eval:           ## frozen behavioral comparison: discovery -> validation -> untouched final split, baseline ladder
	$(PY) -m server.cli eval

demo:           ## seed the clearly labeled demonstration project (synthetic data)
	$(PY) -m server.cli demo

demo-reset:     ## remove local synthetic demo data and reseed
	$(PY) -m server.cli demo --reset

export:         ## manifests, traces and result summary for the demo project
	$(PY) -m server.cli export --project proj_demo

gc:             ## dry-run garbage collection (use `python -m server.cli gc --apply` to delete)
	$(PY) -m server.cli gc

backup:         ## copy database + objects + workspaces to ./backup
	$(PY) -m server.cli backup backup

lock:           ## refresh the Python lockfile from the current venv
	$(PY) -m pip freeze --exclude-editable > requirements.lock
