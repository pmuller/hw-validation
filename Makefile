.PHONY: help sync format lint typecheck test check build clean smoke

PYZ := dist/hw-validation.pyz

help:
	@printf 'Targets:\n'
	@printf '  sync       Install project and dev tools with uv\n'
	@printf '  format     Format Python code with ruff\n'
	@printf '  lint       Run ruff lint checks\n'
	@printf '  typecheck  Run basedpyright\n'
	@printf '  test       Run pytest\n'
	@printf '  check      Run lint, format check, typecheck, and tests\n'
	@printf '  smoke      Run CLI help smoke checks\n'
	@printf '  build      Build shiv deployment package\n'
	@printf '  clean      Remove build artifacts\n'

sync:
	uv sync --all-groups

format:
	uv run ruff format .
	uv run ruff check --fix .

lint:
	uv run ruff check .
	uv run ruff format --check .

typecheck:
	uv run basedpyright

test:
	uv run pytest

check: lint typecheck test

smoke:
	uv run hw-validation --help >/tmp/hw-validation-help.txt
	uv run hw-validation run --help >/tmp/hw-validation-run-help.txt
	uv run hw-validation system --help >/tmp/hw-validation-system-help.txt
	uv run hw-validation disk --help >/tmp/hw-validation-disk-help.txt
	uv run hw-validation logs --help >/tmp/hw-validation-logs-help.txt
	uv run hw-validation readiness --help >/tmp/hw-validation-readiness-help.txt

build: clean
	mkdir -p dist
	uv run shiv --compressed -o $(PYZ) -c hw-validation .
	python3 $(PYZ) --help >/tmp/hw-validation-pyz-help.txt

clean:
	rm -rf build dist .pytest_cache .ruff_cache .mypy_cache
