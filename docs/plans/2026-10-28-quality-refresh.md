# Quality Tooling Refresh Plan

## Work

1. Verify repository identity, instructions, inherited index and worktree changes, subprocess isolation, and Oracle precedent. → verify: retain baseline patches and inspect the declared test helpers and CI matrix.
2. Reproduce the two unsupported `zip` keyword calls with the current code on Python 3.9, then remove only those keywords and configure Ruff for `py39`. → verify: the same pure-code probe fails before the fix and passes afterward.
   Preserve the observed Python 3.9 full-suite failure caused by missing `os.waitid`, then add only the approved capability guard to the unreaped-owner test. → verify: Python 3.9 reports exactly one skip and Python 3.14 executes that test without skips; no assertions or fixtures change.
3. Refresh stale enabled linter pins and keep secret scanning offline with the local filesystem JSONL-to-SARIF reader registered through `direct_configs` and `parser`. → verify: compare enabled pins with the verified inventory, run the reader subprocess regression suite locally and in CI, and prove encoded-only, literal, and clean controls while preserving SARIF output, success codes, and the copied-target sandbox.
4. Run the declared gates sequentially when the shared heavy-job slot is available. → verify: both `make check` interpreter runs and the full no-fix Trunk scan pass.
5. Reconcile affected knowledge and review the candidate without changing the index. → verify: AST equivalence permitting only the exact unreaped-owner capability guard, negative controls for strings, assertions and blanket skips, internal anchors, runtime and CI claims, and `git diff --check`.
6. The root agent reviews the evidence and owns local commits and the publication handoff.

## Declared Gates

- `python3 -B -m unittest discover -s .trunk/configs/tests -p 'test_*.py'`
- `make check PYTHON=/usr/bin/python3`
- `make check PYTHON=/opt/homebrew/bin/python3.14`
- `trunk check --all --no-fix --jobs=2`

`make check` covers `zsh -n histfix.plugin.zsh`, `zsh -n _histfix`, Python AST parsing, ShellCheck of `tests/test-unit.sh`, and the complete isolated test suite.
Set `HISTFIX_PYTHON` to the same interpreter in the child environment so the zsh plugin also exercises the selected runtime.
CI runs Python 3.9 on Linux and Python 3.14 on Linux and macOS, preserving every required branch status context.
Save exact commands, interpreter paths, exits, semantic checks, canary results, and remaining limits in the visible review directory.
