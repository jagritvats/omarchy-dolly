# Dolly - one-command entry points (no build step; pure Python + QML)
PY ?= python3

.PHONY: test demo demo-fast scenarios doctor validate clean

test:            ## unit + integration tests (no Hyprland needed; needs ffmpeg)
	$(PY) -m pytest -q tests/

demo:            ## synthetic end-to-end demo -> ./demo-out (needs playwright + chromium)
	bin/dolly demo demo-out

demo-fast:       ## same at 720p30 (quicker on slow VMs)
	bin/dolly demo demo-out --fast

scenarios:       ## round-3 scenario suite: metrics before/after, clips, contact sheet -> ./scenarios-out (fails if a cap is missed)
	$(PY) harness/run_scenarios.py $(or $(OUT),scenarios-out)

doctor:          ## prerequisite + capture-path report
	bin/dolly doctor

validate:        ## Omarchy's own manifest validator (run on Omarchy)
	omarchy plugin validate .

clean:
	rm -rf demo-out scenarios-out .pytest_cache tests/__pycache__ dolly/__pycache__ dolly/editor/__pycache__
