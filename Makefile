PYTHON ?= python3

.PHONY: check test syntax lint

check: syntax lint test

test:
	$(PYTHON) tests/test_histfix.py

syntax:
	zsh -n histfix.plugin.zsh
	zsh -n _histfix
	$(PYTHON) -c 'import ast; from pathlib import Path; [ast.parse(Path(p).read_text(), filename=p) for p in ("histfix.py", "tests/test_histfix.py")]'

lint:
	shellcheck tests/test-unit.sh
