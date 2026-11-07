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

VENV ?= .venv/bin

# The Go toolchain comes from PATH. Point GO_BIN at a specific one when it is
# installed outside the system path, e.g.
#   make gate-go GO_BIN=$HOME/go-toolchain/go/bin/go
# rather than editing this file: a hardcoded /home/<someone>/ here ships to the
# public mirror and tells the world the maintainer's account name and workspace
# layout. The leak canary on that repo rejects exactly this, and it caught this
# line rather than any of our own checks.
GO_BIN ?= go
# Resolved to an absolute path and handed to pytest as HP_GO_BIN (#726). The
# real-binary journeys used to look for `go` on PATH by themselves, so
# `make gate GO_BIN=...` ran gate-go with that toolchain while gate-py skipped all
# fourteen shipped-binary journeys and still passed. Empty when GO_BIN does not
# resolve; under HP_GATE=1 the suite then REFUSES to run (tests/go_toolchain.py)
# unless HP_GATE_ALLOW_NO_GO=1 names the skip on purpose.
HP_GO_BIN := $(shell command -v $(GO_BIN) 2>/dev/null)
# A per-user build cache, not a fixed /tmp path (#726): a fixed name in a shared
# world-writable directory is clobbered by - or pre-created for - every other
# checkout and user on the box. Go's cache is safe for concurrent use by
# design, so one persistent cache per user stays warm without colliding.
GOCACHE ?= $(or $(XDG_CACHE_HOME),$(HOME)/.cache)/homepilot-gate/go-build
GO_ENV := GOCACHE=$(GOCACHE)

.PHONY: gate gate-py gate-web gate-go gate-go-race gate-image gate-security gate-mirror

gate: gate-py gate-web gate-go gate-go-race gate-security gate-mirror

# The suite in the public mirror's file tree (the scrub's deletions + PROVENANCE).
# The mirror's CI runs the same tests; a test that reads a file the scrub removes
# used to fail only there, after merge (3.6.25, 3.7.0).
gate-mirror: export HP_GATE := 1
gate-mirror: export HP_GO_BIN := $(HP_GO_BIN)
gate-mirror: export GOCACHE := $(GOCACHE)
gate-mirror:
	$(VENV)/python scripts/mirror_tree_check.py
	@echo "gate: all sub-gates passed"

gate-security:
	# Mirror CI's integration job runs these exact audits; a local gate that
	# never runs the command CI runs is not a gate (#548 - bandit and
	# detect-secrets findings kept surfacing only after the ~15 min
	# scrub+mirror+CI round-trip, e.g. #547 and the 3.6.6 B608).
	$(VENV)/bandit -r src/homepilot/ -c pyproject.toml -b .bandit_baseline.json -q
	# A private temp file per run (#726): the fixed /tmp/hp-secrets-report.json
	# was shared by every concurrent run and worktree, so one run could read
	# another's report and pass or fail on the wrong tree.
	@REPORT=$$(mktemp -t hp-secrets-report.XXXXXX) || exit 1; \
	trap 'rm -f "$$REPORT"' EXIT; \
	$(VENV)/detect-secrets scan --all-files --force-use-all-plugins src/ > "$$REPORT" || exit 1; \
	FINDINGS=$$($(VENV)/python -c "import json,sys;print(sum(len(v) for v in json.load(open(sys.argv[1]))['results'].values()))" "$$REPORT"); \
	if [ "$$FINDINGS" != "0" ]; then \
		echo "gate-security: detect-secrets found $$FINDINGS potential secrets:"; \
		$(VENV)/python -c "import json,sys;d=json.load(open(sys.argv[1]))['results'];[print(f'  {f}:{i[\"line_number\"]} {i[\"type\"]}') for f,its in d.items() for i in its]" "$$REPORT"; \
		exit 1; \
	fi
	# The dependency audit mirror CI runs (#726): without it here, a vulnerable
	# pin surfaces only after the scrub+mirror round-trip. Needs network (PyPI/OSV).
	$(VENV)/pip-audit --desc
	@echo "gate-security: bandit + detect-secrets + pip-audit clean"

# THIS tree's src/ first on the path (#653). The shared venv's editable install
# points at whichever checkout ran `pip install -e .`, so without this a
# worktree lane (`make gate-py VENV=<shared>/.venv/bin`) imports the OTHER
# checkout's source and reports green over code that holds none of its changes.
# PYTHONPATH entries precede the `.pth`-added ones on sys.path. The check below
# then PROVES which source is under test instead of assuming it.
gate-py: export PYTHONPATH := $(CURDIR)/src
# HP_GATE=1 turns "no Go toolchain" from a silent skip into a refusal (#726).
gate-py: export HP_GATE := 1
gate-py: export HP_GO_BIN := $(HP_GO_BIN)
gate-py: export GOCACHE := $(GOCACHE)
gate-py:
	$(VENV)/python scripts/check_gate_source.py "$(CURDIR)/src"
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

# The agent is a concurrent program - a reconnect loop, a read loop, a heartbeat
# ticker and a metrics flusher all touching one socket - so "the tests pass" is
# not the same claim as "the shared state is actually guarded". `go test` alone
# runs the reconnect test happily with an unsynchronised read of a.conn; only
# -race reports it. Kept as its own target because the race detector roughly
# doubles the run, and part of `gate` because a torn socket read is the kind of
# defect that only ever shows up in production.
#
# This runs LOCALLY. It is deliberately not a GitHub workflow: this repo is
# private and Actions minutes are billed.
gate-go-race:
	cd agent/go && $(GO_ENV) $(GO_BIN) test -race ./...

gate-image:
	bash scripts/smoke-image.sh
