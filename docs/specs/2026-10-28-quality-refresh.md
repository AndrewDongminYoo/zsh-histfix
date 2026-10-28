# Quality Tooling Refresh

## Approved Scope

Refresh supported dependencies and existing quality tooling.
Preserve the Python 3.9 runtime floor, standard-library-only Python runtime, public APIs, and history behavior.
Keep the selected Trunk CLI, plugin source, and tool runtimes unless a concrete compatibility failure requires a separate decision.
Keep inherited formatter output and remove only the two Python 3.9-incompatible `zip` keyword additions.
Guard only the unreaped-owner test when the interpreter lacks `waitid`, `P_PID`, `WEXITED`, or `WNOWAIT`; keep its assertions and fixtures unchanged.

## Acceptance

- Pinact remains locked to `4.1.1` with `lint` and `upgrade`.
- Enabled linter versions match the verified maintenance inventory, including shfmt 3.14.1 and TruffleHog 3.99.7.
- Ruff targets Python 3.9 and does not reintroduce the unsupported `zip` keyword.
- The existing product and history-test Python ASTs match `HEAD` after normalizing the contiguous standard-library import block and permitting only the exact capability guard on `test_unreaped_killed_owner_recovers_flush_replace_and_undo`; changed string literals, assertions, or blanket skips must fail the comparison.
- `make check` passes with Python 3.9 and 3.14, including native zsh syntax, Bash-only ShellCheck, and the complete isolated history and pseudoterminal tests.
- Apple's Python 3.9.6 lacks `os.waitid`, so only the unreaped-owner test is skipped on that interpreter; Python 3.14 must execute it and pass without skips.
- `trunk check --all --no-fix --jobs=2` passes without scanning zsh as Bash.
- TruffleHog scans offline with `--no-verification` and uses the local filesystem JSONL-to-SARIF reader through `direct_configs` and `parser` while preserving SARIF output, success codes, and the copied-target sandbox.
- The reader preserves decoded-only findings at file level when `Raw` has no literal match, reports available literal locations, fails invalid conversion without emitting clean SARIF, and accepts a clean control.
- `python3 -B -m unittest discover -s .trunk/configs/tests -p 'test_*.py'` passes; CI runs this reader suite on Python 3.14 on Linux and macOS.
- Tooling, CI, and runtime claims in the affected knowledge files match the current configuration.

## History Safety

Never read or write the operator's history or source shell startup files.
Use only disposable history files and `zsh -f` child processes with isolated `HOME` and `ZDOTDIR` values.
Keep the existing isolation assertions, lock algorithms, shell plugin, completion, test assertions, and release version unchanged.

## Publication

Push and PR creation require explicit repository-specific approval.
After approval, continue the requested hosted gates, merge, cleanup and memory recording.
