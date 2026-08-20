.PHONY: test lint

# The family test-lint gate (handbook#80 reusable, wired via
# .github/workflows/test-lint.yml) runs literal `make test` / `make lint` on
# bare runners with no setup-python step. This repo is pinned to Python 3.14
# (UCD 16.0.0 — the suite fails closed on unicodedata drift) and needs PyYAML,
# so under CI the test target provisions itself: resolve a 3.14 interpreter
# (PATH first, then the runner tool cache), build a throwaway venv (hosted
# macOS pythons are PEP 668 externally managed), install requirements, run.
# The venv bin is prepended to PATH for the suite run: tests spawn bin/
# scripts as subprocesses (#!/usr/bin/env python3), and those children must
# resolve the same provisioned 3.14 + PyYAML, not the runner's system python
# (this mirrors what actions/setup-python did for the pre-#17 workflow).
# Locally (CI unset) it stays a thin unittest wrapper on your python3.
test:
	@if [ -n "$$CI" ]; then \
		PY=$$(command -v python3.14 || ls /opt/hostedtoolcache/Python/3.14.*/x64/bin/python3 /Users/runner/hostedtoolcache/Python/3.14.*/*/bin/python3 2>/dev/null | tail -n 1); \
		if [ -z "$$PY" ]; then echo "error: no Python 3.14 interpreter on this runner (UCD 16.0.0 pin) — failing closed" >&2; exit 1; fi; \
		VENV=$$(mktemp -d)/venv && \
		"$$PY" -m venv "$$VENV" && \
		"$$VENV/bin/python" -m pip install --quiet -r requirements.txt && \
		PATH="$$VENV/bin:$$PATH" "$$VENV/bin/python" -m unittest discover -s tests; \
	else \
		python3 -m unittest discover -s tests; \
	fi

# Boundary-drift gate (issue #21): verify growthlane/guard.py's pinned
# default-ignorable ranges against the checked-in UCD 16.0.0 source copy.
# Offline (sha256 of the copy is verified by the script itself), stdlib-only,
# version-independent (literal comparison) — runs on any modern python3.
lint:
	python3 -B bin/pgl-ucd-corpus verify --from data/ucd/DerivedCoreProperties.txt
