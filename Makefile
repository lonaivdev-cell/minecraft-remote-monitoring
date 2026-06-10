# Convenience targets; on Arch prefer `makepkg -si` (see PKGBUILD).

.PHONY: dev test test-all lint install-user install-units clean

dev:            ## editable install + dev deps
	pip install -e ".[dev]"

test:           ## unit tests (fast, no tmux needed)
	python -m pytest -m "not integration"

test-all:       ## everything incl. tmux integration tests
	python -m pytest

lint:
	ruff check src tests

install-user:   ## non-Arch fallback (pipx recommended over this)
	pip install --user .

install-units:  ## user units without pacman (pipx installs)
	mcctl watchdog install

clean:
	rm -rf build dist *.egg-info src/*.egg-info .pytest_cache .ruff_cache pkg
