# zsh-histfix

Find and replace text in zsh history without opening the history file.
Preview each changed entry, then confirm once to apply all replacements.
Entries keep their order and timestamps, and are never merged.

## Setup

Requires zsh 5.9 and Python 3.9 or newer, with no third-party Python packages.
Python handles regular expressions, zsh history encoding, and atomic file replacement.
The zsh function refreshes the calling shell's history after a change.

Clone this repository, then add this after your history configuration in `.zshrc`:

```zsh
source /absolute/path/to/zsh-histfix/histfix.plugin.zsh
```

Set `HISTFIX_PYTHON` if `python3` is not on your `PATH`.
The plugin reads it each time `histfix` runs, so you can set it before or after sourcing.
The plugin can coexist with Oh My Zsh and `zsh-autosuggestions`.
Oh My Zsh users must also change their history options, as described in [Oh My Zsh](#oh-my-zsh).
It does not change key bindings or shell options persistently.
It adds its own directory to `fpath` and registers completion for commands and options, but it never runs `compinit`.
Completion works whether you source the plugin before or after `compinit`.

The [Homebrew tap](https://github.com/AndrewDongminYoo/homebrew-tap) provides a HEAD-only development formula:

```zsh
brew install --HEAD AndrewDongminYoo/tap/histfix
source "$(brew --prefix)/share/histfix/histfix.plugin.zsh"
```

Add the `source` line to `.zshrc` to load the installed plugin in new shells.

### Oh My Zsh

Oh My Zsh loads `plugins/<name>/<name>.plugin.zsh`, so clone into a directory named `histfix`:

```zsh
git clone https://github.com/AndrewDongminYoo/zsh-histfix.git "${ZSH_CUSTOM:-${ZSH:-$HOME/.oh-my-zsh}/custom}/plugins/histfix"
```

A clone in a directory named `zsh-histfix` is reported as `plugin 'zsh-histfix' not found`.

Oh My Zsh enables `SHARE_HISTORY` in its `lib/history.zsh`.
With that option enabled, histfix previews with `--dry-run` but refuses replacement and undo, as described in [History and undo](#history-and-undo).
To apply changes, add `histfix` to `plugins`, disable the option after Oh My Zsh loads, and keep incremental writes:

```zsh
plugins=(git histfix)
source "$ZSH/oh-my-zsh.sh"
unsetopt share_history
setopt inc_append_history
```

Then open a new shell.
`SHARE_HISTORY` also wrote each command to the history file as it was entered.
Without `INC_APPEND_HISTORY`, zsh writes the session's commands only when the shell exits, so a killed terminal loses them.
Open shells no longer import each other's commands at each prompt, and new shells still read the saved history file.

## Usage

Literal matching is the default; dots, dollar signs, and backslashes have no special meaning.
All occurrences in each matching entry are replaced.

```zsh
histfix replace 'gpt-6.1-astra' 'gpt-6-astra'
histfix replace --regex 'gpt-6(?:\.\d+)?-(sol)' 'gpt-6.1-$1'
histfix replace --ignore-case 'GPT-6-SOL' 'gpt-6.1-sol'
histfix replace --dry-run 'gpt-6.1-astra' 'gpt-6-astra'
histfix replace -- '--resumn' '--resume'
histfix undo
histfix --help
```

Use single quotes so the shell does not expand capture references such as `$1`.
Use `--` before find text that starts with a dash.

```plaintext
1 - "codex --model gpt-6.1-astra"
1 + "codex --model gpt-6-astra"
2 - "codex --model gpt-6.1-astra --profile local"
2 + "codex --model gpt-6-astra --profile local"
2 entries would change.
Apply these changes? [y/N]
```

Preview numbers identify records in the file, not zsh event numbers.
Enter `y` or `yes` to apply; any other answer or end-of-input cancels.
`--dry-run` previews without prompting or replacing entries.
Direct calls starting with `histfix` or `noglob histfix` are excluded from replacement, including earlier searches.
Commands are displayed as quoted strings so embedded newlines and terminal control characters are visible.
History entries are never executed by this tool.

## Regex rules

`--regex` uses Python's `re` syntax, not a complete emulation of VS Code's regex engines.
It supports the example above, capture groups, non-capturing groups, anchors, and lookarounds supported by Python.
Each complete command is a separate match subject, including multiline commands.
`^` and `$` refer to the command boundaries unless you enable multiline mode with `(?m)`.

In regex replacement text:

| Token              | Meaning                |
| ------------------ | ---------------------- |
| `$1` through `$99` | Numbered capture group |
| `$0` or `$&`       | Entire match           |
| `$$`               | Literal dollar sign    |

References to missing groups are rejected before writing history.
Unmatched optional groups become empty strings.
Backslashes in replacement text remain literal; named capture references and VS Code case-conversion escapes are not supported.
An empty find string or a replacement that empties an entire command is rejected.

## History and undo

The plugin operates on `$HISTFILE` and requires positive `HISTSIZE` and `SAVEHIST` values.
Replacement and undo work with and without `SHARE_HISTORY`, but the current shell is refreshed differently.

It first exports unsaved commands with zsh's incremental history writer and appends them under a lock, including for a cancelled operation or `--dry-run`.
It then changes only matching records, preserves the history file's permissions, and refreshes the current shell's existing events without changing their order or duplicate counts.
Unchanged records are retained byte for byte, including zsh's Meta encoding.

Without `SHARE_HISTORY`, applying or undoing a replacement requires each current in-memory event to correspond to a record in the history file.
If older, imported, or write-suppressed events exist only in memory, the operation refuses to replace records instead of discarding those events or writing them to disk.
Ambiguous duplicate occurrences and incomplete memory snapshots also cause a refusal.
Preview, `--dry-run`, and cancellation remain available; the initial flush can still append ordinary pending commands.

With `SHARE_HISTORY`, the shell imports other shells' commands at every prompt, so its events cannot be matched to a snapshot.
zsh also keeps a shared-history read position that `fc -R` cannot reset, which would import every record again at the next prompt.
After a successful replacement or undo, the plugin therefore starts a new history level with `fc -p`, which reads the rewritten history file and resets that position.
If the helper could not remove `<HISTFILE>.LOCK` after the change, the plugin skips this reload and reports it, because `fc -p` would wait for that directory indefinitely.
The current shell then shows the history file's records in file order.
Events that existed only in memory, such as commands excluded by `HIST_IGNORE_SPACE` or a `zshaddhistory` hook, are no longer listed.
They are not written to disk.

One undo record is stored beside the resolved history file as `<HISTFILE>.histfix-undo.json`, with mode `0600`.
It contains history content and is replaced on the next successful replacement.
If a filesystem write fails, the previous undo record is restored.
If that restoration also fails, the error identifies a private recovery directory; any prior record retained there is kept for manual recovery.
`histfix undo` restores the previous content while retaining commands appended after the replacement.
If deleting the used undo backup fails, undo still refreshes the current shell and reports the retained backup path as a warning.
If the original final record had no line terminator, flushing or undo adds the required record boundary before retaining appended commands.
Undo refuses if existing records were changed, reordered, or pruned in the meantime.

Close other shells using the same history file before applying replacements.
The tool checks for changes made during preview and takes zsh-compatible history locks during the write, but it cannot refresh another shell's in-memory history.
Another open shell can later save stale commands back to disk.
Do not use another history-rewriting tool concurrently.

### Removing sensitive text

A replacement changes the history file, but copies of the old text can remain elsewhere:

- Other open shells keep the old commands in memory, also with `SHARE_HISTORY`. They show them in their history and can write them back with `fc -W`.
- With `SHARE_HISTORY`, each replacement or undo adds a history level in the current shell, and the level below keeps the old commands until the shell exits. `fc -P` restores that level. Each level holds a full copy of the history list, so memory use grows with each replacement in a long-running shell.
- `zsh-hist`'s `hist undo` runs `fc -P` and then `fc -W`. After a histfix replacement with `SHARE_HISTORY`, it would restore the previous level and write the old commands back to the history file.
- `<HISTFILE>.histfix-undo.json` contains the complete history from before the last replacement until the next replacement or a successful undo. Delete it if you do not need to undo.
- If a histfix process is killed, its private temporary directory under `$TMPDIR` or a `.<history file name>.*` temporary file beside the history file can keep a copy of the history.
- Backups and synchronized copies of the history file are not changed.

To remove a secret, close other shells that use the history file, then delete the undo record after you check the result.
Rotate a leaked credential: removing it from history does not revoke it.

## Abandoned history locks

New histfix locks are private `<HISTFILE>.LOCK` directories (mode `0700`) with
an atomically written `owner.json` (mode `0600`). The versioned owner record
contains the user ID, machine ID, boot ID, PID, kernel process start identity,
and a unique acquisition token. On Linux it also identifies the PID namespace.
A later invocation can recover a recognized lock from this machine when the owner
has exited, the PID belongs to a different process, or the machine has rebooted.
Within the same boot, process probes require the original PID namespace. After a
confirmed reboot of the same machine, old namespaces cannot contain live owners;
recovery also permits a recreated Linux namespace with a different inode.
Lock age alone never establishes abandonment.
macOS uses the platform UUID, boot session UUID, and `proc_pidinfo` start time;
Linux uses the machine ID, boot ID, and `/proc` process start ticks.
If those identities or process probes are unavailable, recovery is refused.

Recovery and writing share a nonblocking advisory lock on the persistent
`<HISTFILE>.histfix-lock` file (mode `0600`). Its kernel lock is released on exit,
including after `SIGKILL`; the file stays in place so waiters cannot lock different
inodes. Do not delete or replace this guard while a writer may be using it.

The `.LOCK` directory also prevents default zsh writers from age-unlinking a lock
between histfix's owner check and removal. Histfix removes recognized owner
metadata through an opened directory descriptor, then uses `rmdir`; this cannot
delete a replacement zsh symlink or regular file. If zsh acquires in the gap before
histfix's exclusive `mkdir`, histfix refuses and preserves the zsh lock.
The helper continues to respect zsh's `HIST_FCNTL_LOCK` advisory lock.

Active owners, zsh symlink/regular/hard-link locks, all regular-file histfix locks,
legacy empty locks, unknown formats, extra directory entries, foreign hosts,
other namespaces within the same boot, and ambiguous process identities are preserved. JSON regular-file
locks from an earlier development version are also manual-recovery cases.
A crash before owner publication or between owner removal and directory removal
can leave an incomplete directory, which is preserved for manual recovery.

Default zsh cannot remove these directories itself. Its history saver may wait,
and after a directory is ten seconds old its lock retry loop can spin until the
directory is removed. After abnormal termination, recover a recognized directory
with a separate histfix invocation before allowing other shells to save, or use
manual recovery for an incomplete or unknown directory. These checks do not
synchronize live shells or make shared network histories safe; close other shells
sharing `HISTFILE` before replacement or undo, as described above.

For manual recovery, first stop all histfix operations and close every shell or
other writer using the resolved history path, including writers on another host
if the file is shared. Back up history and inspect `.LOCK`, its symlink target or
its directory contents and owner information. Only after independently confirming
that no writer is active, remove that specific lock and retry. A directory needs
its inspected owner metadata or interrupted publication files removed before
`rmdir`; preserve any unrecognized contents for inspection. Do not remove a lock
merely because it is old or a PID looks absent on a different host. Keep the
persistent `.histfix-lock` guard; inspect and repair an unrecognized guard only
while all writers are stopped. Lock cleanup errors report the retained path as
a warning and do not prevent a committed change from refreshing current-shell
history, except with `SHARE_HISTORY`: while `.LOCK` remains, that refresh is
skipped, as described in [History and undo](#history-and-undo).

## Development

Tests use temporary files and isolated zsh processes; they do not read the operator's history.

```bash
make check
trunk check --no-fix
```
