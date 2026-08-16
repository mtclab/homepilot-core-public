# HomePilot v2 — local gate runner.
#
# This is the single verification gate for the repo. It is a PRIVATE repo with
# no push/pull_request CI (Actions minutes are billed), so `make gate` run
# locally is the authority — not a workflow. Run it before every push.
#
# Sub-gates run independently: `make gate-py`, `make gate-web`, `make gate-go`.
# Python tools are always invoked through the project venv (`.venv/bin/…`).
#
# `make gate-image` is a PRE-RELEASE shipped-artifact smoke (build the Docker
# image, start it, assert it boots + serves MCP). It needs docker and takes a
# few minutes, so it is NOT part of the default `gate` — run it before releasing.
# The local suite runs against the venv and cannot catch a build-resolution drift
# (see #399); only building the real image can.

VENV := .venv/bin
GO_ENV := GOCACHE=/tmp/gocache GOPATH=/home/kasm-user/go PATH=$$PATH:/home/kasm-user/go-toolchain/go/bin

.PHONY: gate gate-py gate-web gate-go gate-image

gate: gate-py gate-web gate-go
	@echo "gate: all sub-gates passed"

gate-py:
	$(VENV)/ruff check src tests scripts
	$(VENV)/ruff format --check src tests
	$(VENV)/mypy src
	$(VENV)/python -m pytest -q

gate-web:
	cd web && npm run build && npx svelte-check && npx vitest run

gate-go:
	cd agent/go && $(GO_ENV) go vet ./... && $(GO_ENV) go test ./...

gate-image:
	bash scripts/smoke-image.sh
