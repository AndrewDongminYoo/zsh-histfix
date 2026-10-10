# histfix

## Scope

This repository contains a zsh plugin and a Python standard-library helper for editing shell history.
Keep runtime dependencies limited to zsh and Python.
Homebrew packaging is maintained in `AndrewDongminYoo/homebrew-tap`.

## Files

- `histfix.plugin.zsh`: argument validation, incremental history flush, current-shell reload, and completion registration.
- `_histfix`: zsh completion for commands and options.
- `histfix.py`: literal and regex replacements, zsh history encoding, preview, atomic writes, and undo.
- `tests/test_histfix.py`: isolated file, zsh, and pseudoterminal regression tests.
- `docs/specs/` and `docs/plans/`: behavior contracts and implementation plans.
- `docs/notes/`: durable project notes when needed.

## Checks

```bash
make check
trunk check --no-fix
```

Use `trunk fmt` only with explicit paths after reviewing the formatter diff.
Do not run ShellCheck on `.zsh` files; validate them with `zsh -n` and real zsh tests.
CI tests the supported Python floor on Linux and current Python on Linux and macOS.

## History safety

Never test against an operator's real history or source their shell startup files.
Use temporary directories and `zsh -f` with an isolated `HOME` and `ZDOTDIR`.
Preserve record boundaries, timestamps, encoding, permissions, and unrelated commands.
Keep replacement text inert; never evaluate it as shell code.
Keep preview, confirmation, cancellation, conflict detection, and undo covered by tests.
State the limits of cross-shell history synchronization in user documentation.

## Publishing

Use pull requests for feature and CI changes.
The operator controls merges and releases unless explicitly delegated.
Keep the plugin PR and its dependent tap PR linked.

`__version__` in `histfix.py` is the only version number in this repository.
A release is a `vX.Y.Z` tag on `main` whose number matches `__version__`.
After the tag exists, update `url` and `sha256` in the tap's `Formula/histfix.rb` by hand to the tag's source archive.
