# df-multitenant — multi-tenant DeerFlow stack (design doc §9, §11)
#
# DEER_FLOW_ROOT points at the pinned deer-flow checkout (upstream, read-only
# — never edited by this repo). See upstream/PIN.md for the locked ref.

DEER_FLOW_ROOT ?= $(abspath ../deer-flow-demo)
BACKEND := $(DEER_FLOW_ROOT)/backend
BACKEND_PY := $(BACKEND)/.venv/bin/python
PORT_ADMIN ?= 8010
PORT_RUNTIME ?= 8001
FOUNDATION ?= $(CURDIR)/.foundation

.PHONY: install config admin runtime frontend test test-patches smoke frontend-smoke stop clean

install: ## Sync the root workspace + editable-install our packages into the backend venv
	uv sync
	uv pip install --python $(BACKEND_PY) \
		-e packages/tenancy-core \
		-e packages/deerflow-patches \
		-e packages/tenant-db

frontend-install: ## Install frontend dependencies (pnpm)
	cd frontend && pnpm install

frontend: ## Start the Vite dev server (proxies /api -> runtime, /admin -> tenant-admin)
	cd frontend && pnpm dev

frontend-smoke: ## End-to-end frontend smoke (needs admin+runtime+frontend all running)
	cd frontend && node scripts/smoke.mjs http://localhost:5173

FRONTEND_PORT ?= 5173

stop: ## Stop the dev services (admin / runtime / frontend by port)
	@pids=$$(lsof -ti :$(PORT_ADMIN),$(PORT_RUNTIME),$(FRONTEND_PORT) 2>/dev/null); \
	if [ -n "$$pids" ]; then \
		kill $$pids 2>/dev/null || true; sleep 1; \
		kill -9 $$pids 2>/dev/null || true; \
		echo "stopped services on :$(PORT_ADMIN) :$(PORT_RUNTIME) :$(FRONTEND_PORT)"; \
	else \
		echo "no services running"; \
	fi

config: ## Copy the deployment config (models/keys) from the pinned checkout
	@test -f config.yaml || cp $(DEER_FLOW_ROOT)/config.yaml config.yaml
	@sed -i.bak 's|sqlite_dir: .deer-flow/data|sqlite_dir: .foundation/_platform/data|' config.yaml && rm -f config.yaml.bak
	@mkdir -p .foundation/_platform
	@echo "config.yaml ready (database.sqlite_dir -> .foundation/_platform/data)"

# NOTE: every service/test is invoked as `python -m <tool>`. The demo
# checkout's backend .venv is a COPY of ../deer-flow's — its console scripts
# (pytest, uvicorn, ...) carry shebangs pointing back into ../deer-flow's
# venv and would re-exec into the wrong environment. `python -m` always
# uses the venv's real interpreter.

admin: ## Start the tenant-admin service (management plane, no deerflow)
	uv run python -m uvicorn apps.tenant_admin.main:app --app-dir . --port $(PORT_ADMIN)

runtime: ## Start the agent-runtime service (tenant-patched DeerFlow gateway)
	PYTHONPATH="$(CURDIR):$(BACKEND)" \
	DF_FOUNDATION_ROOT="$(FOUNDATION)" \
	DEER_FLOW_PROJECT_ROOT="$(CURDIR)" \
	DEER_FLOW_AUTH_DISABLED=1 \
	TENANT_JWKS_URL="http://127.0.0.1:$(PORT_ADMIN)/.well-known/jwks.json" \
	uv run --project $(BACKEND) \
		python -m uvicorn apps.agent_runtime.main:app --app-dir $(CURDIR) --port $(PORT_RUNTIME)

test: ## Unit suite in the root workspace env (deerflow tests self-skip)
	uv run python -m pytest tests

test-patches: ## Full suite in the backend venv (deerflow importable)
	PYTHONPATH="$(CURDIR):$(BACKEND)" \
	DF_FOUNDATION_ROOT="$(CURDIR)/.foundation-test" \
	uv run --project $(BACKEND) python -m pytest tests

smoke: ## Auth + isolation smoke against a running admin+runtime pair
	@bash scripts/smoke.sh $(PORT_ADMIN) $(PORT_RUNTIME)

clean:
	rm -rf .pytest_cache .runtime .foundation-test
