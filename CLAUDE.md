# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

@AGENTS.md

`AGENTS.md` owns the scope, file roles, check commands, history-safety rules, and publishing rules.
`README.md` owns user-facing behavior, including the lock-recovery contract; read its "Abandoned history locks" section before changing `history_lock` or `recover_history_lock`.
This file adds only what needs several files to understand.

## Running tests

All tests are in one `unittest` class, `HistfixTests`, in `tests/test_histfix.py`.
Run one test by name from any directory:

```bash
python3 tests/test_histfix.py HistfixTests.test_regex_capture_groups_and_all_occurrences
```

The test helpers use three levels of isolation:

- `run_helper` runs `histfix.py` as a subprocess with `HISTFIX_FILE` set and no memory snapshot, so it tests the file rewrite alone.
- `run_zsh` runs `zsh -f -i -c SCRIPT fixture HISTORY PLUGIN`; in the script, `$1` is the temporary history file and `$2` is the plugin.
- `interactive_zsh` drives a real `zsh -f -i` through a pseudoterminal for the prompt-time behavior that `-c` cannot reach.

`load_helper` imports `histfix.py` as a module so that tests can patch internals such as `atomic_write` and the process probes.
The native macOS identity test runs only on `darwin`; CI covers Linux on Python 3.9 and 3.14.

Two fixture traps:

- `run_zsh` and `interactive_zsh` record their setup line, which contains the temporary path under `/var/folders`. A literal replacement of `old` also rewrites that path, so use distinctive tokens such as `token-old` in tests that replace text.
- The macOS terminal driver takes Ctrl-T as its status key, so a pseudoterminal test cannot bind it in zle. The completion test binds Ctrl-B instead.

## Plugin and helper protocol

`histfix.plugin.zsh` and `histfix.py` communicate through exit codes and environment variables.
A change to one side usually needs a matching change to the other.

| Exit code | Constant            | Meaning to the plugin                                                          |
| --------- | ------------------- | ------------------------------------------------------------------------------ |
| 0         | none                | Done: help, version, no match, dry run, or cancellation; memory stays as it is |
| 2         | none                | Error; the message is already on stderr                                        |
| 10        | `APPLIED`           | History file changed; reload the current shell's history                       |
| 11        | `VALIDATED`         | `--validate` accepted a command that can write                                 |
| 12        | `DRY_RUN_VALIDATED` | `--validate` accepted a dry run, the only command allowed with an empty list   |
| 130       | none                | Interrupted                                                                    |

The plugin saves the caller's `HISTSIZE` and `SAVEHIST`, then shadows `SAVEHIST` with a local maximum so that `fc -AI` never trims while exporting.
One `histfix` call then runs the helper up to three times:

1. `--validate ARGS` parses arguments only, before any history write, so `--help`, `--version`, and usage errors work without `HISTFILE`.
2. `--flush PENDING` appends the commands that `fc -AI` exported from a subshell, under the history lock. The parent shell then runs `fc -AI` again only to mark those events as saved after the append succeeded. `fc -AI` writes no file for an empty history list; the plugin then refuses unless step 1 returned 12, and runs a dry run as an empty flush followed by the preview.
3. The real command runs with `HISTFIX_FILE`. The reload after exit code 10 depends on `SHARE_HISTORY`.

Without `SHARE_HISTORY`, step 3 also passes `HISTFIX_MEMORY` (a `fc -W` snapshot taken with an empty `HISTORY_IGNORE`) and `HISTFIX_HISTORY_COUNT`.
`prepare_memory_reload` maps each in-memory event to a record in the history file and rewrites the snapshot, and refuses when an event exists only in memory or when duplicates make the mapping ambiguous.
On exit code 10 the plugin clears memory with `HISTSIZE=0` and then reads the snapshot back with `HISTSIZE` capped at the event count plus one, so the active `histfix` event is evicted and the original order is kept.

With `SHARE_HISTORY`, other shells' imports change the list at every prompt, so the plugin passes no snapshot.
`fc -R` cannot reset zsh's shared-history read position, so on exit code 10 the plugin restores the caller's `SAVEHIST` and runs `fc -p "$HISTFILE"` with the saved values.
That pushes a new history level on every replacement or undo; `fc -P` would restore the level below and its old text.
`fc -p` takes zsh's history lock first and waits forever on a directory lock, so the plugin skips the reload while `<HISTFILE>.LOCK` remains.
The spec's 2026-10-10 reconciliation and the README section "Removing sensitive text" record the consequences.

## Files that the helper writes beside `HISTFILE`

All paths derive from the resolved history path:

- `<HISTFILE>.histfix-undo.json`: one undo record that holds base64 `before` and `after` content; `commit_replacement` replaces it and restores the previous record on failure.
- `<HISTFILE>.LOCK`: a private lock directory with `owner.json`, compatible with zsh's own lock name.
- `<HISTFILE>.histfix-lock`: a persistent `fcntl` guard file; never delete it in code or tests while a writer can run.

Record parsing keeps zsh Meta encoding: `records`, `unmeta`, and `metafy` operate on bytes, and unchanged records must be written back byte for byte.
