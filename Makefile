VENV ?= .venv
BIN = $(VENV)/bin

.PHONY: test lint typecheck check clean schemas

test:
	$(BIN)/pytest --cov=workspace --cov=workspace_mcp --cov-report=term-missing

lint:
	$(BIN)/ruff check workspace workspace_mcp tests

typecheck:
	$(BIN)/mypy workspace workspace_mcp

check: lint typecheck test

schemas:
	$(BIN)/workspace-mcp --generate-schemas ~/.gemini/antigravity/mcp/google-workspace

clean:
	rm -rf .coverage .pytest_cache .mypy_cache .ruff_cache build dist *.egg-info
