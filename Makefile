# astronaut-streamer: run `make` for the list of targets.
#
# The same targets work from billee-hardware-2027's root as
# `make astronaut <target>`. Full guide: docs/SETUP.md.

SHELL := /bin/bash
.DEFAULT_GOAL := help
.PHONY: help jetson hotspot view pi probe test-pattern push-pi status logs restart uninstall test lint e2e

SENDER := astronaut-sender
UNITS  := astronaut-mediamtx astronaut-web astronaut-sender
E2E_VENV ?= .e2e-venv

# Run $(1) with the sender service stopped, because it holds the camera and
# mic and would fight a second publisher, then start it again afterwards
# (Ctrl-C included).
define with_sender_paused
if systemctl is-active --quiet $(SENDER) 2>/dev/null; then \
  echo "Pausing $(SENDER) (it holds the camera and mic)..."; \
  sudo systemctl stop $(SENDER); \
  trap 'echo "Starting $(SENDER) again..."; sudo systemctl start $(SENDER)' EXIT; \
  trap 'exit 130' INT TERM; \
fi; \
$(1)
endef

help: ## Show this list
	@echo "astronaut-streamer: Pi camera -> Jetson hotspot -> ground-station browser"
	@echo "Guide: docs/SETUP.md    Test checklist: docs/TEST_CHECKLIST.md"
	@echo
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | \
	  awk 'BEGIN {FS = ":.*?## "} {printf "  \033[36m%-13s\033[0m %s\n", $$1, $$2}'

# ------------------------------------------------------------------ Jetson
jetson: ## [Jetson] install all: packages, MediaMTX, dashboard, services, hotspot
	sudo bash jetson/jetson_install_me.sh
	sudo bash jetson/hotspot.sh up

hotspot: ## [Jetson] hotspot status (change: sudo bash jetson/hotspot.sh up|start|stop|remove)
	@bash jetson/hotspot.sh status

view: ## [Jetson] watch + listen on the Jetson's own screen (bench testing)
	@bash jetson/local_view.sh

# ---------------------------------------------------------------------- Pi
pi: ## [Pi] install all: packages, Wi-Fi, SSH, hotspot profile, sender service
	sudo bash pi/pi_install_me.sh

probe: ## [Pi] bench check: camera, mic, speaker tone, encoders, Wi-Fi
	@$(call with_sender_paused,python3 pi/probe_devices.py --beep)

test-pattern: ## [Pi] send colour bars + 440 Hz tone instead of the camera (Ctrl-C stops)
	@$(call with_sender_paused,python3 pi/sender.py --test-pattern)

push-pi: ## [laptop] push this commit to the Pi through the Jetson (no internet), restart it
	@bash tools/push_pi.sh

# ------------------------------------------------------------------ either
status: ## Services, hotspot/Wi-Fi and stream state on this machine
	@bash tools/status.sh

logs: ## Follow this machine's service logs (Ctrl-C stops)
	@units=$$(for u in $(UNITS); do [ -f /etc/systemd/system/$$u.service ] && printf -- '-u %s ' $$u; done); \
	  if [ -z "$$units" ]; then echo "No astronaut-streamer services on this machine."; \
	  else journalctl -f -n 30 $$units; fi

restart: ## Restart this machine's services
	@found=; for u in $(UNITS); do \
	  if [ -f /etc/systemd/system/$$u.service ]; then sudo systemctl restart $$u && echo "Restarted $$u"; found=1; fi; \
	done; [ -n "$$found" ] || echo "No astronaut-streamer services on this machine."

uninstall: ## Remove this machine's services (keeps packages, Wi-Fi setup, settings)
	sudo bash tools/uninstall.sh

test: ## Unit tests (any machine, no hardware needed)
	python3 -m unittest discover -s tests

e2e: ## [laptop] whole pipeline locally: MediaMTX + dashboard + test sender + headless Chrome
	@E2E_VENV="$(E2E_VENV)" bash tools/e2e_setup.sh
	@"$(E2E_VENV)/bin/python" tools/e2e_local.py

lint: ## Shellcheck the scripts (if installed) and byte-compile the Python
	@if command -v shellcheck >/dev/null; then \
	  shellcheck -x config/config.sh jetson/*.sh pi/*.sh tools/*.sh && echo "shellcheck: clean"; \
	else echo "shellcheck not installed (sudo apt install shellcheck); skipped"; fi
	@python3 -m py_compile common/*.py pi/*.py jetson/*.py tools/*.py && echo "python: compiles"
