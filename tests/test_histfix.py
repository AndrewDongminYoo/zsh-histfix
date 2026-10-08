"""Exercise replacements against disposable files and a real zsh history reader."""

import os
import importlib.util
import io
from contextlib import contextmanager
from pathlib import Path
import pty
import select
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "histfix.py"
PLUGIN = ROOT / "histfix.plugin.zsh"


class HistfixTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.history = self.home / "history"
        self.env = dict(os.environ, HISTFIX_FILE=str(self.history))

    def run_helper(self, *args, answer="y\n"):
        return subprocess.run(
            [sys.executable, str(HELPER), *args],
            input=answer, text=True, capture_output=True, env=self.env, timeout=10,
        )

    def write(self, content):
        self.history.write_bytes(content)
        self.history.chmod(0o600)

    def test_literal_replaces_records_without_merging_or_editing_its_own_calls(self):
        self.write(
            b": 100:2;codex --model gpt-6.1-astra\n"
            b": 101:0;histfix replace 'gpt-6.1-astra' 'gpt-6-astra'\n"
            b": 102:3;codex --model gpt-6.1-astra\n"
            b": 103:0;echo gpt-6x1-astra\n"
        )
        result = self.run_helper("replace", "gpt-6.1-astra", "gpt-6-astra")
        self.assertEqual(result.returncode, 10, result.stderr)
        self.assertIn("2 entries", result.stdout)
        self.assertEqual(self.history.read_bytes(),
            b": 100:2;codex --model gpt-6-astra\n"
            b": 101:0;histfix replace 'gpt-6.1-astra' 'gpt-6-astra'\n"
            b": 102:3;codex --model gpt-6-astra\n"
            b": 103:0;echo gpt-6x1-astra\n")
        self.assertEqual(self.history.stat().st_mode & 0o777, 0o600)

    def test_regex_capture_groups_and_all_occurrences(self):
        self.write(b"codex gpt-6-sol gpt-6.2-sol\necho gpt-6-astra\n")
        result = self.run_helper(
            "replace", "--regex", r"gpt-6(?:\.\d+)?-(sol)", "gpt-6.1-$1"
        )
        self.assertEqual(result.returncode, 10, result.stderr)
        self.assertEqual(self.history.read_bytes(),
                         b"codex gpt-6.1-sol gpt-6.1-sol\necho gpt-6-astra\n")

    def test_cancel_dry_run_noop_and_invalid_input_do_not_change_history(self):
        original = b"echo old\n"
        cases = [
            (("replace", "old", "new"), "n\n", 0),
            (("replace", "old", "new"), "", 0),
            (("replace", "--dry-run", "old", "new"), "", 0),
            (("replace", "absent", "new"), "y\n", 0),
            (("replace", "old", "old"), "y\n", 0),
            (("replace", "--regex", "[", "new"), "y\n", 2),
            (("replace", "--regex", "(old)", "$2"), "y\n", 2),
            (("replace", "", "new"), "y\n", 2),
            (("replace", "--unknown", "old", "new"), "y\n", 2),
        ]
        for args, answer, expected_exit in cases:
            with self.subTest(args=args, answer=answer):
                self.write(original)
                result = self.run_helper(*args, answer=answer)
                self.assertEqual(result.returncode, expected_exit, result.stderr)
                self.assertEqual(self.history.read_bytes(), original)
                self.assertFalse(Path(str(self.history) + ".histfix-undo.json").exists())

    def test_undo_keeps_entries_appended_after_replacement(self):
        original = b": 100:1;echo old\n"
        self.write(original)
        result = self.run_helper("replace", "old", "new")
        self.assertEqual(result.returncode, 10, result.stderr)
        with self.history.open("ab") as stream:
            stream.write(b": 101:2;echo later\n")
        result = self.run_helper("undo")
        self.assertEqual(result.returncode, 10, result.stderr)
        self.assertEqual(self.history.read_bytes(), original + b": 101:2;echo later\n")
        self.assertNotEqual(self.run_helper("undo").returncode, 10)

    def test_undo_refuses_to_overwrite_unrelated_edits(self):
        self.write(b"echo old\n")
        self.assertEqual(self.run_helper("replace", "old", "new").returncode, 10)
        self.write(b"echo someone-else\n")
        result = self.run_helper("undo")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(self.history.read_bytes(), b"echo someone-else\n")

    def test_undo_preserves_record_boundary_after_unterminated_original(self):
        for original, restored in (
            (b"echo old", b"echo old\n"),
            (b"echo old\\", b"echo old\\ \n"),
        ):
            with self.subTest(original=original):
                self.write(original)
                result = self.run_helper("replace", "old", "new")
                self.assertEqual(result.returncode, 10, result.stderr)
                with self.history.open("ab") as stream:
                    stream.write(b"echo later\n")
                result = self.run_helper("undo")
                self.assertEqual(result.returncode, 10, result.stderr)
                self.assertEqual(self.history.read_bytes(), restored + b"echo later\n")

    def test_undo_recognizes_normalized_unchanged_final_record(self):
        pending = self.home / "pending"
        pending.write_bytes(b"echo later\n")
        for tail, normalized in (
            (b"echo tail", b"echo tail\n"),
            (b"echo tail\\", b"echo tail\\ \n"),
            (b": tail", b"\\: tail\n"),
        ):
            with self.subTest(tail=tail):
                self.write(b"echo old\n" + tail)
                self.assertEqual(self.run_helper("replace", "old", "new").returncode, 10)
                result = self.run_helper("--flush", str(pending))
                self.assertEqual(result.returncode, 0, result.stderr)
                result = self.run_helper("undo")
                self.assertEqual(result.returncode, 10, result.stderr)
                self.assertEqual(self.history.read_bytes(),
                                 b"echo old\n" + normalized + b"echo later\n")

    def run_zsh(self, script, *args):
        # -f skips the operator's rc files. All history paths belong to this test.
        return subprocess.run(
            ["zsh", "-f", "-i", "-c", script, "fixture", str(self.history),
             str(PLUGIN), *args],
            text=True, capture_output=True, timeout=15,
            env=dict(self.env, HOME=str(self.home), ZDOTDIR=str(self.home)),
        )

    def test_plugin_updates_memory_and_preserves_unsaved_history(self):
        self.write(b": 100:1;codex gpt-6.1-astra\n")
        result = self.run_zsh('''
HISTFILE=$1
HISTSIZE=100
SAVEHIST=100
setopt EXTENDED_HISTORY
fc -R "$HISTFILE"
source "$2" || exit 90
print -s -- 'echo unsaved'
histfix replace 'gpt-6.1-astra' 'gpt-6-astra' <<< y || exit 91
print -s -- 'reader-sentinel'
print -r -- "MEMORY:${(j:|:)history}"
fc -AI "$HISTFILE"
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        memory = result.stdout.split("MEMORY:")[-1]
        self.assertIn("codex gpt-6-astra", memory)
        self.assertNotIn("gpt-6.1-astra", memory)
        self.assertIn("echo unsaved", memory)
        self.assertIn(b"echo unsaved", self.history.read_bytes())

    def test_plugin_preserves_existing_bytes_when_flushing_pending_commands(self):
        original = b"echo old\n: 100:2;echo untouched\necho untouched\n"
        for action, answer, expected in (
            ("replace old new", "y", original.replace(b"old", b"new")),
            ("replace old new", "n", original),
            ("replace --dry-run old new", "", original),
        ):
            with self.subTest(action=action, answer=answer):
                self.write(original)
                result = self.run_zsh('''
HISTFILE=$1
HISTSIZE=100
SAVEHIST=100
fc -R "$HISTFILE"
source "$2" || exit 90
print -s -- 'echo pending'
histfix ${=3} <<< "$4" || exit 91
histfix replace --dry-run absent new || exit 92
HISTFILE=''
''', action, answer)
                self.assertEqual(result.returncode, 0, result.stderr)
                content = self.history.read_bytes()
                self.assertTrue(content.startswith(expected), repr(content))
                self.assertEqual(content.count(b"echo pending"), 1, repr(content))

    def test_plugin_retains_memory_only_history_without_replacement(self):
        imported = self.home / "imported"
        imported.write_bytes(
            b": 90:1;echo memory-only-1\n"
            b": 91:1;echo memory-only-2\n"
            b": 92:1;echo memory-only-3\n"
        )
        for action, answer in (
            ("replace absent new", ""),
            ("replace --dry-run old new", ""),
            ("replace old new", "n"),
        ):
            with self.subTest(action=action, answer=answer):
                self.write(b": 100:1;echo disk-old\n")
                result = self.run_zsh('''
HISTFILE=$1
HISTSIZE=100
SAVEHIST=10
fc -R "$3"
fc -R "$HISTFILE"
print -s -- 'fixture-active'
source "$2" || exit 90
histfix ${=4} <<< "$5" || exit 91
print -s -- 'fixture-after'
print -r -- "MEMORY:${(j:|:)history}"
HISTFILE=''
''', str(imported), action, answer)
                self.assertEqual(result.returncode, 0, result.stderr)
                memory = result.stdout.split("MEMORY:", 1)[1].splitlines()[0]
                for number in (1, 2, 3):
                    self.assertIn(f"echo memory-only-{number}", memory)
                self.assertIn("echo disk-old", memory)
                self.assertNotIn(b"memory-only", self.history.read_bytes())

    def test_apply_and_undo_refuse_to_discard_memory_only_events(self):
        imported = self.home / "imported"
        imported.write_bytes(b": 90:1;echo memory-only\n")
        for action in ("replace old new", "undo"):
            with self.subTest(action=action):
                self.write(b": 100:1;echo disk-old\n")
                if action == "undo":
                    self.assertEqual(self.run_helper("replace", "old", "new").returncode, 10)
                expected = self.history.read_bytes()
                result = self.run_zsh('''
HISTFILE=$1; HISTSIZE=100; SAVEHIST=10
fc -R "$3"; fc -R "$HISTFILE"
print -s -- 'fixture-active'
source "$2"
histfix ${=4} <<< y
print -r -- "RESULT:$?"
print -s -- 'reader-sentinel'
print -r -- "MEMORY:${(j:|:)history}"
HISTFILE=''
''', str(imported), action)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("RESULT:2", result.stdout)
                self.assertIn("memory-only", result.stderr)
                self.assertIn("echo memory-only", result.stdout)
                self.assertTrue(self.history.read_bytes().startswith(expected))
                self.assertNotIn(b"memory-only", self.history.read_bytes())

    def test_reload_preserves_memory_order_and_duplicate_occurrences(self):
        self.write(b": 100:1;echo old\n: 101:1;echo old\n: 102:1;echo keep\n")
        result = self.run_zsh('''
HISTFILE=$1; HISTSIZE=100; SAVEHIST=100
setopt EXTENDED_HISTORY
fc -R "$HISTFILE"
print -s -- fixture-active
source "$2"
( fc -W "$1.before" )
histfix replace old new <<< y || exit 91
( fc -W "$1.applied" )
histfix undo <<< y || exit 92
( fc -W "$1.restored" )
[[ $HISTSIZE == 100 && $SAVEHIST == 100 ]] || exit 93
HISTFILE=''
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        before = Path(str(self.history) + ".before").read_bytes()
        self.assertEqual(Path(str(self.history) + ".applied").read_bytes(),
                         before.replace(b"echo old", b"echo new"))
        self.assertEqual(Path(str(self.history) + ".restored").read_bytes(), before)

    def test_undo_refuses_ambiguous_memory_occurrences(self):
        self.write(b": 100:1;echo old\n: 100:1;echo new\n")
        result = self.run_zsh('''
HISTFILE=$1; HISTSIZE=100; SAVEHIST=100
setopt EXTENDED_HISTORY
fc -R "$HISTFILE"
print -s -- fixture-active
source "$2"
histfix replace old new <<< y || exit 91
( fc -W "$1.applied" )
histfix undo <<< y
[[ $? == 2 ]] || exit 92
( fc -W "$1.refused" )
HISTFILE=''
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ambiguous duplicates", result.stderr)
        self.assertEqual(Path(str(self.history) + ".applied").read_bytes(),
                         Path(str(self.history) + ".refused").read_bytes())
        self.assertNotIn(b"echo old", self.history.read_bytes())
        self.assertTrue(Path(str(self.history) + ".histfix-undo.json").exists())

    def test_failed_flush_retains_pending_commands_for_retry(self):
        self.write(b"echo existing\n")
        result = self.run_zsh('''
HISTFILE=$1
HISTSIZE=100
SAVEHIST=100
fc -R "$HISTFILE"
source "$2" || exit 90
print -s -- 'echo pending'
touch "$HISTFILE.LOCK"
histfix replace --dry-run absent new
[[ $? == 2 ]] || exit 91
[[ $(cat "$HISTFILE") == 'echo existing' ]] || exit 92
rm "$HISTFILE.LOCK"
histfix replace --dry-run absent new || exit 93
histfix replace --dry-run absent new || exit 94
HISTFILE=''
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        content = self.history.read_bytes()
        self.assertTrue(content.startswith(b"echo existing\n"), repr(content))
        self.assertEqual(content.count(b"echo pending"), 1, repr(content))

    def test_zsh_reads_changed_multiline_unicode_and_trailing_backslash_entries(self):
        # The fixture is written by zsh, including its non-UTF-8 Meta byte encoding.
        result = self.run_zsh('''
HISTSIZE=100
SAVEHIST=100
setopt EXTENDED_HISTORY
print -s -- $'echo 한글 old\\nprintf old'
print -s -- 'echo old\\'
print -s -- ': old'
fc -W "$1"
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_helper("replace", "old", "새값")
        self.assertEqual(result.returncode, 10, result.stderr)
        result = self.run_zsh('''
HISTSIZE=100
fc -R "$1"
print -s -- 'reader-sentinel'
print -rn -- "${(pj:\\0:)history}"
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertCountEqual(result.stdout.split("\0"),
                              ["echo 한글 새값\nprintf 새값", "echo 새값\\", ": 새값"])

    def test_zsh_reads_terminal_newlines_without_added_padding(self):
        result = self.run_zsh('''
HISTSIZE=100
SAVEHIST=100
print -s -- $'printf old\\n'
print -s -- $'printf old\\n\\n'
fc -W "$1"
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_helper("replace", "old", "new")
        self.assertEqual(result.returncode, 10, result.stderr)
        result = self.run_zsh('''
HISTSIZE=100
fc -R "$1"
print -s -- 'reader-sentinel'
print -rn -- "${(pj:\\0:)history}"
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertCountEqual(result.stdout.split("\0"), ["printf new\n", "printf new\n\n"])

    def test_fcntl_lock_is_held_until_history_replacement(self):
        spec = importlib.util.spec_from_file_location("histfix_under_test", HELPER)
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        original_write = helper.atomic_write
        probe = '''import fcntl, sys
with open(sys.argv[1], 'r+b') as stream:
    try:
        fcntl.lockf(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(1)
'''

        checked_targets = []

        def checked_write(target, data, mode, **kwargs):
            if target == self.history.resolve():
                checked_targets.append(target)
                result = subprocess.run([sys.executable, "-c", probe, str(target)],
                                        capture_output=True, timeout=10)
                self.assertEqual(result.returncode, 1, "fcntl lock released before write")
            original_write(target, data, mode, **kwargs)

        pending = self.home / "pending"
        pending.write_bytes(b"echo later\n")
        for args in (("replace", "old", "new"), ("--flush", str(pending))):
            with self.subTest(args=args):
                self.write(b"echo old\n")
                with mock.patch.dict(os.environ, HISTFIX_FILE=str(self.history)), \
                        mock.patch.object(helper, "atomic_write", side_effect=checked_write), \
                        mock.patch.object(helper, "confirm", return_value=True):
                    helper.main(list(args))
        self.assertEqual(len(checked_targets), 2)

    def test_preview_race_refuses_to_overwrite_a_new_command(self):
        self.write(b"echo old\n")
        with subprocess.Popen(
            [sys.executable, str(HELPER), "replace", "old", "new"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=self.env,
        ) as process:
            self.assertIn('"echo old"', process.stdout.readline())
            self.assertIn('"echo new"', process.stdout.readline())
            self.assertIn("1 entries", process.stdout.readline())
            self.write(b"echo old\necho concurrent\n")
            _, error = process.communicate("y\n", timeout=10)
            self.assertEqual(process.returncode, 2, error)
            self.assertIn("changed during preview", error)
        self.assertEqual(self.history.read_bytes(), b"echo old\necho concurrent\n")

    def test_existing_zsh_lock_blocks_replacement(self):
        self.write(b"echo old\n")
        lock = Path(str(self.history) + ".LOCK")
        lock.symlink_to("/pid-999999/host-fixture")
        result = self.run_helper("replace", "old", "new")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("locked", result.stderr)
        self.assertTrue(lock.is_symlink())
        self.assertEqual(self.history.read_bytes(), b"echo old\n")

    def test_permission_change_during_preview_refuses_replacement(self):
        self.write(b"echo old\n")
        self.history.chmod(0o644)
        with subprocess.Popen(
            [sys.executable, str(HELPER), "replace", "old", "new"],
            env=self.env, text=True, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ) as process:
            self.assertIn('1 - "echo old"', process.stdout.readline())
            self.assertIn('1 + "echo new"', process.stdout.readline())
            self.assertIn("1 entries", process.stdout.readline())
            self.history.chmod(0o600)
            _, error = process.communicate("y\n", timeout=10)
            self.assertEqual(process.returncode, 2, error)
            self.assertIn("changed during preview", error)
        self.assertEqual(self.history.read_bytes(), b"echo old\n")
        self.assertEqual(self.history.stat().st_mode & 0o777, 0o600)

    def test_history_group_and_mode_survive_replace_undo_and_flush(self):
        self.write(b"echo old\n")
        groups = [gid for gid in os.getgroups() if gid != self.history.stat().st_gid]
        if not groups:
            self.skipTest("requires a supplementary group")
        os.chown(self.history, -1, groups[0])
        self.history.chmod(0o640)
        pending = self.home / "pending"
        pending.write_bytes(b"echo later\n")
        for args, expected in ((('replace', 'old', 'new'), 10), (('undo',), 10),
                               (('--flush', str(pending)), 0)):
            with self.subTest(args=args):
                result = self.run_helper(*args)
                self.assertEqual(result.returncode, expected, result.stderr)
                info = self.history.stat()
                self.assertEqual(info.st_gid, groups[0])
                self.assertEqual(info.st_mode & 0o777, 0o640)

    def test_flush_uses_metadata_from_the_locked_file(self):
        spec = importlib.util.spec_from_file_location("histfix_under_test", HELPER)
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        original_lock = helper.history_lock
        pending = self.home / "pending"
        pending.write_bytes(b"echo later\n")
        groups = [os.getgid()] + [group for group in os.getgroups() if group != os.getgid()][:1]
        for group in groups:
            with self.subTest(group=group):
                self.write(b"echo old\n")
                self.history.chmod(0o644)

                @contextmanager
                def changed_metadata_lock(target):
                    os.chown(target, -1, group)
                    target.chmod(0o600)
                    with original_lock(target) as stream:
                        yield stream

                with mock.patch.dict(os.environ, HISTFIX_FILE=str(self.history)), \
                        mock.patch.object(helper, "history_lock", changed_metadata_lock):
                    self.assertEqual(helper.main(["--flush", str(pending)]), 0)
                self.assertEqual(self.history.read_bytes(), b"echo old\necho later\n")
                self.assertEqual(self.history.stat().st_mode & 0o777, 0o600)
                self.assertEqual(self.history.stat().st_gid, group)

    def test_failed_history_write_preserves_prior_undo(self):
        spec = importlib.util.spec_from_file_location("histfix_under_test", HELPER)
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        original_write = helper.atomic_write
        backup = Path(str(self.history) + ".histfix-undo.json")

        def fail_history_write(target, data, mode, **kwargs):
            if target == self.history.resolve():
                raise OSError("injected history write failure")
            original_write(target, data, mode, **kwargs)

        for prior_undo in (False, True):
            with self.subTest(prior_undo=prior_undo):
                self.write(b"echo old\n")
                if prior_undo:
                    self.assertEqual(self.run_helper("replace", "old", "one").returncode, 10)
                expected_history = self.history.read_bytes()
                expected_undo = backup.read_bytes() if prior_undo else None
                with mock.patch.dict(os.environ, HISTFIX_FILE=str(self.history)), \
                        mock.patch.object(helper, "atomic_write", side_effect=fail_history_write), \
                        mock.patch.object(helper, "confirm", return_value=True):
                    with self.assertRaisesRegex(OSError, "injected history write failure"):
                        helper.main(["replace", "one" if prior_undo else "old", "two"])
                self.assertEqual(self.history.read_bytes(), expected_history)
                if prior_undo:
                    self.assertEqual(backup.read_bytes(), expected_undo)
                    self.assertEqual(self.run_helper("undo").returncode, 10)
                    self.assertEqual(self.history.read_bytes(), b"echo old\n")
                else:
                    self.assertFalse(backup.exists())

    def test_undo_preparation_failure_does_not_mutate_history_or_backup(self):
        spec = importlib.util.spec_from_file_location("histfix_under_test", HELPER)
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        backup = Path(str(self.history) + ".histfix-undo.json")
        for phase in ("link", "backup"):
            with self.subTest(phase=phase):
                self.write(b"echo old\n")
                self.assertEqual(self.run_helper("replace", "old", "one").returncode, 10)
                expected_history = self.history.read_bytes()
                expected_undo = backup.read_bytes()
                operation = (mock.patch.object(helper.os, "link", side_effect=OSError("link failure"))
                             if phase == "link" else
                             mock.patch.object(helper, "atomic_write", side_effect=OSError("backup failure")))
                with operation, mock.patch.dict(os.environ, HISTFIX_FILE=str(self.history)), \
                        mock.patch.object(helper, "confirm", return_value=True):
                    with self.assertRaisesRegex(OSError, phase + " failure"):
                        helper.main(["replace", "one", "two"])
                self.assertEqual(self.history.read_bytes(), expected_history)
                self.assertEqual(backup.read_bytes(), expected_undo)
                self.assertEqual(list(self.home.glob(".histfix-undo-*")), [])

    def test_undo_recovery_failure_retains_the_previous_inode(self):
        spec = importlib.util.spec_from_file_location("histfix_under_test", HELPER)
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        original_write = helper.atomic_write
        original_replace = helper.os.replace
        backup = Path(str(self.history) + ".histfix-undo.json")
        self.write(b"echo old\n")
        self.assertEqual(self.run_helper("replace", "old", "one").returncode, 10)
        expected_history = self.history.read_bytes()
        expected_undo = backup.read_bytes()
        expected_inode = backup.stat().st_ino

        def fail_history_write(target, data, mode, **kwargs):
            if target == self.history.resolve():
                raise OSError("history write failure")
            original_write(target, data, mode, **kwargs)

        def fail_restore(source, target):
            if Path(source).name == "previous":
                raise OSError("undo restore failure")
            original_replace(source, target)

        with mock.patch.dict(os.environ, HISTFIX_FILE=str(self.history)), \
                mock.patch.object(helper, "atomic_write", side_effect=fail_history_write), \
                mock.patch.object(helper.os, "replace", side_effect=fail_restore), \
                mock.patch.object(helper, "confirm", return_value=True):
            with self.assertRaisesRegex(OSError, "undo recovery failed; retained files:") as error:
                helper.main(["replace", "one", "two"])
        self.assertEqual(self.history.read_bytes(), expected_history)
        recovery, = self.home.glob(".histfix-undo-*")
        self.assertIn(str(recovery), str(error.exception))
        self.assertEqual((recovery / "previous").read_bytes(), expected_undo)
        self.assertEqual((recovery / "previous").stat().st_ino, expected_inode)

    def test_cleanup_failure_after_commit_keeps_the_new_undo(self):
        spec = importlib.util.spec_from_file_location("histfix_under_test", HELPER)
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        original_rmdir = Path.rmdir
        self.write(b"echo old\n")
        self.assertEqual(self.run_helper("replace", "old", "one").returncode, 10)

        def fail_cleanup(directory):
            if directory.name.startswith(".histfix-undo-"):
                raise OSError("cleanup failure")
            original_rmdir(directory)

        with mock.patch.dict(os.environ, HISTFIX_FILE=str(self.history)), \
                mock.patch.object(Path, "rmdir", fail_cleanup), \
                mock.patch.object(helper, "confirm", return_value=True), \
                mock.patch.object(sys, "stderr", new=io.StringIO()) as error:
            self.assertEqual(helper.main(["replace", "one", "two"]), 10)
            self.assertIn("could not remove undo recovery directory", error.getvalue())
        self.assertEqual(self.history.read_bytes(), b"echo two\n")
        self.assertEqual(self.run_helper("undo").returncode, 10)
        self.assertEqual(self.history.read_bytes(), b"echo one\n")

    def test_plain_history_colon_and_control_characters_round_trip(self):
        self.write(b"echo old\rkeep\n\\: old\necho old\\ \n")
        result = self.run_helper("replace", "old", "new")
        self.assertEqual(result.returncode, 10, result.stderr)
        self.assertEqual(self.history.read_bytes(), b"echo new\rkeep\n\\: new\necho new\\ \n")
        self.assertIn(r"\rkeep", result.stdout)

    def test_preview_escapes_c1_terminal_control_characters(self):
        original = b"echo \xc2\x83\xbbold\n"
        self.write(original)
        result = self.run_helper("replace", "--dry-run", "old", "new")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("\u009b", result.stdout)
        self.assertIn(r"\u009b", result.stdout)
        self.assertEqual(self.history.read_bytes(), original)

    def test_literal_replacement_does_not_expand_captures_or_execute_commands(self):
        self.write(b"echo old old\n")
        injected = f"$(touch {self.home / 'executed'}) $1"
        result = self.run_helper("replace", "old", injected)
        self.assertEqual(result.returncode, 10, result.stderr)
        self.assertEqual(self.history.read_bytes(), f"echo {injected} {injected}\n".encode())
        self.assertFalse((self.home / "executed").exists())

    def test_regex_ignore_case_and_literal_dollar(self):
        self.write(b"echo OLD old\n")
        result = self.run_helper("replace", "--regex", "--ignore-case", "(old)", "$$-$1-$&")
        self.assertEqual(result.returncode, 10, result.stderr)
        self.assertEqual(self.history.read_bytes(), b"echo $-OLD-OLD $-old-old\n")

    def test_metadata_and_encoded_nul_are_preserved(self):
        self.write(b": 100:-100;echo 100 \x83 \n")
        result = self.run_helper("replace", "100", "200")
        self.assertEqual(result.returncode, 10, result.stderr)
        self.assertEqual(self.history.read_bytes(), b": 100:-100;echo 200 \x83 \n")

    def test_help_and_invalid_regex_do_not_flush_pending_shell_history(self):
        self.write(b"echo existing\n")
        result = self.run_zsh('''
HISTFILE=$1
HISTSIZE=100
SAVEHIST=100
source "$2" || exit 90
print -s -- 'echo pending'
histfix --help || exit 91
histfix replace --regex '[' replacement
[[ $? == 2 ]] || exit 92
HISTFILE=''
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.history.read_bytes(), b"echo existing\n")

    @contextmanager
    def interactive_zsh(self, configure):
        master, slave = pty.openpty()
        process = subprocess.Popen(
            ["zsh", "-f", "-i"], stdin=slave, stdout=slave, stderr=slave,
            start_new_session=True,
            env=dict(self.env, HOME=str(self.home), ZDOTDIR=str(self.home), TERM="dumb"),
        )
        os.close(slave)

        def send(command):
            os.write(master, command.encode() + b"\n")

        def until(marker):
            output = b""
            deadline = time.monotonic() + 10
            while marker not in output:
                if time.monotonic() >= deadline:
                    self.fail(f"zsh did not emit {marker!r}: {output!r}")
                if select.select([master], [], [], 0.1)[0]:
                    output += os.read(master, 65536)
            return output

        try:
            send(f"unsetopt zle; HISTFILE={shlex.quote(str(self.history))}; "
                 "HISTSIZE=100; SAVEHIST=100; "
                 f'fc -R "$HISTFILE"; source {shlex.quote(str(PLUGIN))}; '
                 f"{configure}; PROMPT='HF''> '")
            until(b"HF> ")
            yield send, until, process
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            os.close(master)

    def test_interactive_prompt_replacement_and_undo_refresh_suggestions(self):
        for option in ("", "INC_APPEND_HISTORY", "INC_APPEND_HISTORY_TIME"):
            with self.subTest(option=option):
                self.write(b": 100:1;codex --model gpt-6.1-astra\n")
                with self.interactive_zsh(f"setopt EXTENDED_HISTORY {option}") as (send, until, process):
                    send("histfix replace 'gpt-6.1-astra' 'gpt-6-astra'")
                    until(b"[y/N] ")
                    send("y")
                    self.assertIn(b"History updated.", until(b"HF> "))
                    send('print -r -- "SUGGESTION:${history[(r)codex*]}"')
                    self.assertIn(b"SUGGESTION:codex --model gpt-6-astra\r\n", until(b"HF> "))
                    send("histfix undo")
                    until(b"[y/N] ")
                    send("y")
                    self.assertIn(b"Replacement undone.", until(b"HF> "))
                    send('print -r -- "SUGGESTION:${history[(r)codex*]}"')
                    self.assertIn(b"SUGGESTION:codex --model gpt-6.1-astra\r\n", until(b"HF> "))
                    send("exit")
                    self.assertEqual(process.wait(timeout=5), 0)
                    self.assertTrue(self.history.read_bytes().startswith(
                        b": 100:1;codex --model gpt-6.1-astra\n"))

    def test_interactive_refusal_keeps_write_suppressed_events_private(self):
        self.env["HISTFIX_TEST_IGNORE"] = "*secret*"
        for configure in (
            "HISTORY_IGNORE=$HISTFIX_TEST_IGNORE",
            "zshaddhistory() { [[ $1 == $~HISTFIX_TEST_IGNORE ]] && return 2; return 0 }",
        ):
            with self.subTest(configure=configure):
                self.write(b": 100:1;echo disk-old\n")
                with self.interactive_zsh(configure) as (send, until, process):
                    send("echo secret-one")
                    until(b"HF> ")
                    send("histfix replace disk-old disk-new")
                    until(b"[y/N] ")
                    send("y")
                    self.assertIn(b"memory-only history", until(b"HF> "))
                    send('print -r -- "RETAINED:${history[(r)echo s*]}"; fc -AI "$HISTFILE"')
                    self.assertIn(b"RETAINED:echo secret-one\r\n", until(b"HF> "))
                    data = self.history.read_bytes()
                    self.assertIn(b"echo disk-old", data)
                    self.assertNotIn(b"echo disk-new", data)
                    self.assertNotIn(b"echo secret-one", data)
                    self.assertFalse(Path(str(self.history) + ".histfix-undo.json").exists())
                    send('HISTFILE=""; exit')
                    self.assertEqual(process.wait(timeout=5), 0)

    def test_share_history_refuses_writes_but_allows_preview_without_duplicates(self):
        for action, expected_code in (
            ("histfix replace old new", 2),
            ("histfix undo", 2),
            ("histfix replace -- old --dry-run", 2),
            ("histfix replace --dry-run old new", 0),
        ):
            with self.subTest(action=action):
                self.write(b": 100:1;echo old\n")
                if action == "histfix undo":
                    self.assertEqual(self.run_helper("replace", "old", "new").returncode, 10)
                with self.interactive_zsh("setopt EXTENDED_HISTORY SHARE_HISTORY") as (send, until, process):
                    send('cp "$HISTFILE" "$HISTFILE.before-disk"; '
                         '( fc -W "$HISTFILE.before-memory" ); '
                         f'{action} <<< y; histfix_exit=$?; '
                         '( fc -W "$HISTFILE.after-memory" ); '
                         'print -r -- "CODE:$histfix_exit OPTION:$options[sharehistory]"')
                    output = until(b"HF> ")
                    self.assertIn(f"CODE:{expected_code} OPTION:on\r\n".encode(), output)
                    if expected_code:
                        self.assertIn(b"cannot apply changes with SHARE_HISTORY", output)
                    else:
                        self.assertIn(b"entries would change.", output)
                    self.assertEqual(self.history.read_bytes(),
                                     Path(str(self.history) + ".before-disk").read_bytes())
                    self.assertEqual(Path(str(self.history) + ".before-memory").read_bytes(),
                                     Path(str(self.history) + ".after-memory").read_bytes())
                    for _ in range(2):
                        send(":")
                        until(b"HF> ")
                    send('fc -W "$HISTFILE.later-memory"')
                    until(b"HF> ")
                    later = Path(str(self.history) + ".later-memory").read_bytes()
                    self.assertEqual(later.count(action.encode()), 1, repr(later))
                    self.assertEqual(later.count(b"PROMPT='HF''> '"), 1, repr(later))
                    send('HISTFILE=""; exit')
                    self.assertEqual(process.wait(timeout=5), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
