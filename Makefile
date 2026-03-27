.PHONY: help check fix lint typecheck security test test-unit test-e2e diff-cover
.DEFAULT_GOAL := help

# Colors and symbols
GREEN  := \033[0;32m
RED    := \033[0;31m
CYAN   := \033[0;36m
BOLD   := \033[1m
RESET  := \033[0m

# Verbose flag: make check V=1 for full output
V ?= 0
ifeq ($(V),1)
  QUIET :=
  TEST_FLAGS := --cov --cov-fail-under=80 -v
else
  QUIET := > /dev/null 2>&1
  TEST_FLAGS := --cov --cov-fail-under=80 -q --no-header
endif

help: ## Show available targets
	@printf "\n$(BOLD)$(CYAN)Available targets:$(RESET)\n\n"
	@grep -h -E '^[a-zA-Z0-9_-]+:.* ## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.* ## "}; {printf "  $(GREEN)%-14s$(RESET) %s\n", $$1, $$2}'
	@printf "\n  Use $(BOLD)V=1$(RESET) for verbose output (e.g. $(CYAN)make check V=1$(RESET))\n\n"

# All checks (mirrors CI — run before pushing a PR)
check: ## Run all checks (lint + typecheck + security + test)
	@printf "\n$(BOLD)$(CYAN)🔍 Running all checks...$(RESET)\n"
	@$(MAKE) --no-print-directory lint
	@$(MAKE) --no-print-directory typecheck
	@$(MAKE) --no-print-directory security
	@$(MAKE) --no-print-directory test
	@printf "\n$(BOLD)$(CYAN)🎉 All checks passed!$(RESET)\n\n"

# Lint (includes format check)
lint: ## Lint and format check (ruff)
	@printf "\n$(BOLD)$(CYAN)🧹 Linting...$(RESET)\n"
	@uv run ruff check . $(QUIET) && printf "  $(GREEN)✅ ruff lint$(RESET)\n" || { printf "  $(RED)❌ ruff lint$(RESET)\n"; exit 1; }
	@uv run ruff format --check . $(QUIET) && printf "  $(GREEN)✅ ruff format$(RESET)\n" || { printf "  $(RED)❌ ruff format$(RESET)\n"; exit 1; }

# Type checking
typecheck: ## Type check (pyright)
	@printf "\n$(BOLD)$(CYAN)🔬 Type checking...$(RESET)\n"
	@uv run pyright $(QUIET) && printf "  $(GREEN)✅ pyright$(RESET)\n" || { printf "  $(RED)❌ pyright$(RESET)\n"; exit 1; }

# Security scan
security: ## Security scan (bandit)
	@printf "\n$(BOLD)$(CYAN)🛡️  Security scan...$(RESET)\n"
	@uv run bandit -r src/ -q $(QUIET) && printf "  $(GREEN)✅ bandit$(RESET)\n" || { printf "  $(RED)❌ bandit$(RESET)\n"; exit 1; }

# Tests with coverage
test: ## Run unit + e2e tests with coverage
	@printf "\n$(BOLD)$(CYAN)🧪 Testing...$(RESET)\n"
	@COV=$$(uv run pytest tests/copilot_usage tests/test_docs.py $(TEST_FLAGS) 2>&1); \
		if [ $$? -eq 0 ]; then \
			COV_PCT=$$(echo "$$COV" | grep "^TOTAL" | awk '{print $$NF}' | tr -d '%'); \
			printf "  $(GREEN)✅ unit tests ($${COV_PCT}%% coverage)$(RESET)\n"; \
		else \
			printf "  $(RED)❌ unit tests$(RESET)\n"; exit 1; \
		fi
	@E2E=$$(uv run pytest tests/e2e -q --no-header 2>&1); \
		if [ $$? -eq 0 ]; then \
			PASSED=$$(echo "$$E2E" | tail -1 | grep -oE '[0-9]+ passed' | awk '{print $$1}'); \
			printf "  $(GREEN)✅ e2e tests ($${PASSED} passed)$(RESET)\n"; \
		else \
			printf "  $(RED)❌ e2e tests$(RESET)\n"; exit 1; \
		fi

# Run only unit tests
test-unit: ## Run unit tests only (verbose)
	@printf "\n$(BOLD)$(CYAN)🧪 Unit tests...$(RESET)\n"
	@uv run pytest tests/copilot_usage -v --cov --cov-fail-under=80

# Run only e2e tests
test-e2e: ## Run e2e tests only (verbose)
	@printf "\n$(BOLD)$(CYAN)🧪 E2E tests...$(RESET)\n"
	@uv run pytest tests/e2e -v

# Auto-fix
fix: ## Auto-fix lint and format issues
	@printf "\n$(BOLD)$(CYAN)🔧 Auto-fixing...$(RESET)\n"
	@uv run ruff check --fix . 2>/dev/null; uv run ruff format . $(QUIET)
	@printf "  $(GREEN)✅ ruff fix + format$(RESET)\n"
	@printf "\n$(BOLD)$(CYAN)✨ All fixed!$(RESET)\n\n"

# Diff coverage (useful in PRs to enforce new-code coverage)
diff-cover: ## Show coverage of changed lines vs main branch
	@printf "\n$(BOLD)$(CYAN)📊 Diff coverage...$(RESET)\n"
	@uv run pytest tests/copilot_usage tests/test_docs.py $(TEST_FLAGS) --cov-report=xml
	@uv run diff-cover coverage.xml --compare-branch=main $(QUIET) && printf "  $(GREEN)✅ diff-cover$(RESET)\n" || { printf "  $(RED)❌ diff-cover$(RESET)\n"; exit 1; }
