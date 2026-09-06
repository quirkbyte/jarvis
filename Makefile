# JARVIS. Every phase's acceptance test is one command from here.
#
# Python: 3.11+ is required (config.py and the whole codebase assume it).
# Override with `make PYTHON=/path/to/python3.12 install` if the pick is wrong.

VENV    := .venv
STATE   ?= $(HOME)/.jarvis
PY      := $(VENV)/bin/python
PIP     := $(VENV)/bin/pip
PYTHON  ?= $(shell for v in python3.14 python3.13 python3.12 python3.11 python3; do \
             command -v $$v >/dev/null 2>&1 && \
             $$v -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)' 2>/dev/null && \
             { command -v $$v; break; }; done)

.PHONY: help install doctor run stop text hud test lint bench clean fixtures voices
.DEFAULT_GOAL := help

help:
	@echo "make install   venv, brew deps, python deps, wake word model"
	@echo "make doctor    check this Mac and name the fix for anything missing"
	@echo "make test      pytest"
	@echo "make lint      ruff check + format"
	@echo "make run       the assistant"
	@echo "make stop      stop it, including copies older than the run lock"
	@echo "make text      text mode, no microphone"
	@echo "make hud       open the running HUD in Chrome, app mode"
	@echo "make bench     latency over fixtures"
	@echo "make voices    audition British voices and pick one"
	@echo "make fixtures  rebuild the test WAVs with macOS say"

$(PY):
	@test -n "$(PYTHON)" || { echo "no python 3.11+ found: brew install python@3.12"; exit 1; }
	$(PYTHON) -m venv $(VENV)
	$(PIP) install --quiet --upgrade pip

install: $(PY)
	@command -v brew >/dev/null 2>&1 \
	  && { brew list portaudio >/dev/null 2>&1 || brew install portaudio; } \
	  || echo "! homebrew missing — sounddevice will not import; see make doctor"
	$(PIP) install --quiet -r requirements.txt
	@echo "downloading the hey_jarvis model (idempotent)..."
	@$(PY) -c "import openwakeword.utils as u; u.download_models()"
	@echo "install done — now run: make doctor"

doctor: $(PY)
	@$(PY) scripts/doctor.py $(ARGS)

test: $(PY)
	$(PY) -m pytest -q

lint: $(PY)
	$(PY) -m ruff check .
	$(PY) -m ruff format --check .

run: $(PY)
	$(PY) -m jarvis

text: $(PY)
	$(PY) -m jarvis --text

# The HUD lives inside the core process (jarvis/hud/server.py shares its
# event loop) — this target does not start anything, it only opens Chrome in
# app mode pointed at wherever one is already serving. `make run` or
# `python scripts/hud_demo.py` is what actually brings the HUD up.
hud:
	@open -na "Google Chrome" --args \
		--app=http://127.0.0.1:8765 \
		--window-size=1440,900 \
		--user-data-dir=/tmp/jarvis-hud

bench: $(PY)
	@$(PY) scripts/bench.py $(ARGS)

clean:
	rm -rf $(VENV) .pytest_cache .ruff_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +

# The lock file holds the pid, so stopping it never needs a pgrep pattern —
# which is how two orphans survived three rounds of "pkill" that matched
# nothing, because the executable is `Python` and the pattern said `python`.
stop:
	@pid=$$(cat $(STATE)/jarvis.pid 2>/dev/null); \
	if [ -n "$$pid" ] && kill -0 $$pid 2>/dev/null; then \
	  echo "stopping jarvis ($$pid)"; kill $$pid; \
	  for i in 1 2 3 4 5 6 7 8 9 10; do kill -0 $$pid 2>/dev/null || break; sleep 0.5; done; \
	  kill -0 $$pid 2>/dev/null && { echo "did not stop; forcing"; kill -9 $$pid; } || true; \
	else echo "jarvis is not running"; fi
	@stragglers=$$(ps -eo pid=,args= | awk '/[Pp]ython[^ ]* -m jarvis$$/ {print $$1}'); \
	if [ -n "$$stragglers" ]; then \
	  echo "also stopping copies that predate the lock: $$stragglers"; kill $$stragglers; \
	fi; true

fixtures: $(PY)
	@$(PY) scripts/fixtures.py

voices: $(PY)
	@$(PY) scripts/voices.py $(ARGS)
