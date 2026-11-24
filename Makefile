# Targets mirror the commands in AGENTS.md. `make help` lists them.
# Everything runs offline: tests and evals use no key and no network.
.DEFAULT_GOAL := help
PY ?= uv run
SHELL_FILES = docker/entrypoint.sh docker/backup/*.sh scripts/*.sh .claude/hooks/*.sh
EVAL_OUT = evals/results/recorded-latest.json

.PHONY: help install fast test one changed lint fmt types shell docs codemap codemap-check \
        gates gate-light evals evals-fake demo scan migrate migration-check prompts-snapshot

help: ## list the targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  make %-16s %s\n", $$1, $$2}'

install: ## install dependencies from the lock file
	uv sync --locked

fast: ## fast loop: no endpoint tests, no slow files, stop at the first failure (about 1 minute)
	$(PY) pytest -q -x -p no:cacheprovider -m "not endpoint and not slow"

test: ## the whole suite (about 3.5 minutes), quiet output
	$(PY) pytest -q -p no:cacheprovider

one: ## run one test: make one T=tests/test_money.py::test_name
	$(PY) pytest -q -x -p no:cacheprovider $(T)

changed: ## run only the tests your uncommitted changes can affect (scripts/select_tests.py)
	@sel="$$($(PY) python scripts/select_tests.py --worktree)"; \
	case "$$sel" in \
	  NONE) echo "No test is affected by the current changes." ;; \
	  ALL) echo "Shared code changed: running everything."; $(PY) pytest -q -p no:cacheprovider ;; \
	  *) echo "$$sel" | tr '\n' ' '; echo; $(PY) pytest -q -x -p no:cacheprovider $$sel ;; \
	esac

lint: ## ruff check and format check
	$(PY) ruff check .
	$(PY) ruff format --check .

fmt: ## apply ruff fixes and formatting
	$(PY) ruff check --fix .
	$(PY) ruff format .

types: ## mypy (strict on orbit/domain and orbit/services)
	$(PY) mypy

shell: ## shellcheck the shell scripts
	shellcheck -e SC1091 $(SHELL_FILES)

docs: ## dead paths, links, make targets and agent config in the docs
	$(PY) python scripts/check_docs.py

codemap: ## regenerate docs/CODEMAP.md tables, ROUTES.md, CONFIG.md, TESTMAP.md
	$(PY) python scripts/gen_codemap.py

codemap-check: ## fail when the generated docs are stale
	$(PY) python scripts/gen_codemap.py --check

gates: lint types shell docs codemap-check scan ## all gates: ruff, mypy, shellcheck, docs, codemap, scan, pytest, offline evals
	$(PY) pytest -q -p no:cacheprovider
	$(MAKE) evals

gate-light: ## quick pre-commit style check (about 10 seconds): lint, docs, codemap, scan
	$(PY) ruff check .
	$(PY) ruff format --check .
	$(PY) python scripts/check_docs.py
	$(PY) python scripts/gen_codemap.py --check
	scripts/scan-personal.sh

evals: ## recorded evals: no key, no network, must be 100 percent; the committed baseline must be current
	$(PY) python -m evals.run --mode recorded --min-pass-rate 1.0 --json $(EVAL_OUT) | tail -12
	diff -u evals/results/baseline-recorded.json $(EVAL_OUT)
	@echo "evals: baseline is current"

evals-fake: ## evals against the offline rule-based FakeClaude (shows how far rules get)
	$(PY) python -m evals.run --mode fake

demo: ## seed a throwaway database, boot the server with fake Claude, check it, stop it
	scripts/demo_smoke.sh

scan: ## personal-data and secret scan of the repository files
	scripts/scan-personal.sh

migrate: ## upgrade the (scratch) database to head
	$(PY) alembic upgrade head

migration-check: ## fail if the models changed without a migration (runs the migration tests)
	$(PY) pytest -q -p no:cacheprovider tests/test_migrations.py

prompts-snapshot: ## record prompt hashes after a deliberate prompt change (also bump the version)
	$(PY) python -m orbit.cli prompts-snapshot
