# Laya Service -- developer and operator entry points.
#
# Everything runs through the project-local virtualenv at .venv. No target
# installs anything system-wide and no target requires sudo.
#
# NOTE: this project directory contains a space ("laya service"). All recipes
# that reference paths quote them. Targets use relative paths because make
# always runs with the Makefile's directory as cwd.

.DEFAULT_GOAL := help
SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c

VENV        := .venv
PY          := $(VENV)/bin/python
PIP         := $(VENV)/bin/pip
UVICORN     := $(VENV)/bin/uvicorn
PYTEST      := $(VENV)/bin/pytest
RUFF        := $(VENV)/bin/ruff
MYPY        := $(VENV)/bin/mypy
PRE_COMMIT  := $(VENV)/bin/pre-commit

# Package index configuration. The Aliyun mirror is used for wheels because it
# is ~25x faster than upstream from this network; PyPI is kept as a fallback
# because the mirror lags on newly published packages (notably `laya` itself).
PIP_INDEX   := https://mirrors.aliyun.com/pypi/simple
PIP_EXTRA   := https://pypi.org/simple
PIP_FLAGS   := --index-url $(PIP_INDEX) --extra-index-url $(PIP_EXTRA)

# CPU-only PyTorch. Installing this BEFORE the project avoids pulling the CUDA
# build (~2.5 GB plus nvidia-* wheels) onto a machine that cannot use it.
TORCH_VER   := 2.9.1+cpu
TORCH_URL   := https://mirrors.aliyun.com/pytorch-wheels/cpu/torch-2.9.1%2Bcpu-cp310-cp310-manylinux_2_28_x86_64.whl

APP         := laya_service.main
HOST        ?= 127.0.0.1
PORT        ?= 9800

.PHONY: help venv install install-dev install-torch lint format format-check \
        typecheck test test-cov check run start stop restart status logs smoke \
        docker-build docker-run docker-stop pre-commit clean clean-all

help: ## Show this help
	@echo "Laya Service -- available targets:"
	@echo
	@grep -hE '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| sort \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'
	@echo

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

$(VENV)/bin/activate:
	python3 -m venv --without-pip $(VENV)
	curl -sS https://bootstrap.pypa.io/get-pip.py -o /tmp/laya-get-pip.py
	$(PY) /tmp/laya-get-pip.py --no-warn-script-location -q

venv: $(VENV)/bin/activate ## Create the virtualenv (bootstraps pip if needed)

install-torch: venv ## Install CPU-only PyTorch (avoids the 2.5 GB CUDA build)
	@if $(PY) -c "import torch" 2>/dev/null; then \
		echo "torch already installed: $$($(PY) -c 'import torch; print(torch.__version__)')"; \
	else \
		echo "downloading CPU-only torch $(TORCH_VER) ..."; \
		curl -sS -o "/tmp/torch-$(TORCH_VER)-cp310-cp310-manylinux_2_28_x86_64.whl" "$(TORCH_URL)"; \
		$(PIP) install -q "/tmp/torch-$(TORCH_VER)-cp310-cp310-manylinux_2_28_x86_64.whl"; \
	fi

install: venv install-torch ## Install runtime dependencies
	$(PIP) install $(PIP_FLAGS) -e .

install-dev: venv install-torch ## Install runtime + development dependencies
	$(PIP) install $(PIP_FLAGS) -e ".[dev]"
	@$(PRE_COMMIT) install --install-hooks >/dev/null 2>&1 || \
		echo "note: pre-commit hooks not installed (run 'pre-commit install' manually)"

# ---------------------------------------------------------------------------
# Quality
# ---------------------------------------------------------------------------

lint: ## Lint with ruff
	$(RUFF) check src tests

format: ## Auto-fix and format with ruff
	$(RUFF) check --fix src tests
	$(RUFF) format src tests

format-check: ## Verify formatting without modifying files
	$(RUFF) format --check src tests

typecheck: ## Static type check with mypy (strict)
	$(MYPY) src tests

test: ## Run the test suite
	$(PYTEST)

test-cov: ## Run tests with a coverage report and the 70% gate
	$(PYTEST) --cov --cov-report=term-missing --cov-report=html

check: lint format-check typecheck test ## Run every quality gate

pre-commit: ## Run all pre-commit hooks against every file
	$(PRE_COMMIT) run --all-files

# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

run: ## Run in the foreground with autoreload (development)
	$(UVICORN) $(APP):create_app --factory --reload --host $(HOST) --port $(PORT)

start: ## Start in the background, writing .run/laya.pid and logs/laya.log
	./scripts/start.sh

stop: ## Stop the background service
	./scripts/stop.sh

restart: stop start ## Restart the background service

status: ## Show process, port, and readiness status
	./scripts/status.sh

logs: ## Tail the service log
	./scripts/logs.sh

smoke: ## Run the post-start smoke test against a running service
	./scripts/smoke_test.sh

# ---------------------------------------------------------------------------
# Containers
# ---------------------------------------------------------------------------

docker-build: ## Build the production image
	docker build -f deploy/docker/Dockerfile -t laya-service:latest .

docker-run: ## Run the image, mapping PORT and passing .env
	docker run -d --name laya-service \
		--env-file .env \
		-p $(PORT):8000 \
		--restart unless-stopped \
		laya-service:latest

docker-stop: ## Stop and remove the container
	-docker rm -f laya-service

# ---------------------------------------------------------------------------
# Housekeeping
# ---------------------------------------------------------------------------

clean: ## Remove caches and build artefacts
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage coverage.xml
	find . -type d -name __pycache__ -not -path './.venv/*' -prune -exec rm -rf {} +
	find . -type f -name '*.py[co]' -not -path './.venv/*' -delete
	rm -rf build dist src/*.egg-info

clean-all: clean ## Also remove the virtualenv and runtime state
	rm -rf $(VENV) .run logs
