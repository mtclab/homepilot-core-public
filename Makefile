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

# The Go toolchain comes from PATH. Point GO_BIN at a specific one when it is
# installed outside the system path, e.g.
#   make gate-go GO_BIN=$HOME/go-toolchain/go/bin/go
# rather than editing this file: a hardcoded /home/<someone>/ here ships to the
# public mirror and tells the world the maintainer's account name and workspace
# layout. The leak canary on that repo rejects exactly this, and it caught this
# line rather than any of our own checks.
GO_BIN ?= go
GOCACHE ?= /tmp/gocache
GO_ENV := GOCACHE=$(GOCACHE)

.PHONY: gate gate-py gate-web gate-go gate-image

gate: gate-py gate-web gate-go
	@echo "gate: all sub-gates passed"

gate-py:
	$(VENV)/ruff check src tests scripts
	$(VENV)/ruff format --check src tests
	$(VENV)/mypy src
	$(VENV)/python -m pytest -q

gate-web:
	# `npm ci --dry-run` FIRST, because it is the only step here that checks the
	# lockfile against package.json. `npm run build` and vitest happily use
	# whatever node_modules is already on disk, so a lockfile that `npm ci`
	# rejects passes this gate while breaking BOTH the Docker image build and
	# public CI, which install with `npm ci`. A dependency refresh produced
	# exactly that once (an unsatisfiable transitive picomatch pin) and it was
	# caught by CI on a release sync rather than here.
	cd web && npm ci --dry-run >/dev/null
	# `--fail-on-warnings`, NOT `--threshold warning`: the threshold flag only
	# filters what is PRINTED and still exits 0, so it looks like a gate and is
	# not one (found by reverting a fixed label and watching the "gate" pass).
	# The warnings here are the a11y ones - an unassociated <label>, a click
	# handler on a non-interactive element. Eleven had accumulated silently
	# because the gate only failed on errors, which is how "accessible" quietly
	# stops being true (#445 B5).
	cd web && npm run build && npx svelte-check --fail-on-warnings && npx vitest run

gate-go:
	cd agent/go && $(GO_ENV) $(GO_BIN) vet ./... && $(GO_ENV) $(GO_BIN) test ./...

gate-image:
	bash scripts/smoke-image.sh
