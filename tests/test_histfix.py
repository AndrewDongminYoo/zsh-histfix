"""Exercise replacements against disposable files and a real zsh history reader."""

import os
import importlib.util
import io
import json
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
        self.env = dict(os.environ, HISTFIX_FILE=str(self.history), HISTFILE="")

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

    @contextmanager
    def child_history_lock(self, mask=None):
        code = """import importlib.util, os, pathlib, sys
spec = importlib.util.spec_from_file_location('helper', sys.argv[1])
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)
if sys.argv[3] != 'None':
    os.umask(int(sys.argv[3]))
with helper.history_lock(pathlib.Path(sys.argv[2])):
    print('ACQUIRED', flush=True)
    sys.stdin.readline()
"""
        with subprocess.Popen(
            [sys.executable, "-c", code, str(HELPER), str(self.history), str(mask)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=dict(self.env, HOME=str(self.home), ZDOTDIR=str(self.home)),
        ) as child:
            try:
                ready, _, _ = select.select([child.stdout], [], [], 10)
                self.assertTrue(ready, "lock child did not become ready")
                self.assertEqual(child.stdout.readline(), "ACQUIRED\n")
                yield child
            finally:
                if child.poll() is None:
                    child.communicate("release\n", timeout=10)

    def test_killed_owner_recovers_flush_replace_and_undo(self):
        self.write(b": 100:1;echo old\n: 101:2;echo untouched\n")
        pending = self.home / "pending"
        pending.write_bytes(b": 102:3;echo later\n")
        lock = Path(str(self.history) + ".LOCK")
        for args, expected in (
            (("--flush", str(pending)), 0),
            (("replace", "old", "new"), 10),
            (("undo",), 10),
        ):
            before = self.history.read_bytes()
            with self.child_history_lock() as child:
                child.kill()
                child.wait(timeout=10)
                self.assertEqual(child.returncode, -9)
            self.assertTrue(lock.exists())
            self.assertEqual(self.history.read_bytes(), before)
            result = self.run_helper(*args)
            self.assertEqual(result.returncode, expected, result.stderr)
            self.assertFalse(lock.exists())
        self.assertEqual(self.history.read_bytes(),
                         b": 100:1;echo old\n: 101:2;echo untouched\n: 102:3;echo later\n")

    def test_unreaped_killed_owner_recovers_flush_replace_and_undo(self):
        self.write(b": 100:1;echo old\n: 101:2;echo untouched\n")
        pending = self.home / "pending"
        pending.write_bytes(b": 102:3;echo later\n")
        lock = Path(str(self.history) + ".LOCK")
        for args, expected in ((("--flush", str(pending)), 0),
                               (("replace", "old", "new"), 10), (("undo",), 10)):
            with self.subTest(args=args), self.child_history_lock() as child:
                child.kill()
                # Wait for actual exit without reaping: PID and zombie persist.
                exited = os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOWAIT)
                self.assertEqual(exited.si_pid, child.pid)
                self.assertIsNone(child.returncode)
                os.kill(child.pid, 0)
                self.assertTrue(lock.exists())
                result = self.run_helper(*args)
                self.assertEqual(result.returncode, expected, result.stderr)
                self.assertFalse(lock.exists())
                # Recovery must not reap the original parent's child.
                os.kill(child.pid, 0)
        self.assertEqual(self.history.read_bytes(),
                         b": 100:1;echo old\n: 101:2;echo untouched\n: 102:3;echo later\n")

    def load_helper(self):
        spec = importlib.util.spec_from_file_location("histfix_lock_test", HELPER)
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        return helper

    def write_owner_lock(self, owner):
        lock = Path(str(self.history) + ".LOCK")
        lock.mkdir(mode=0o700, exist_ok=True)
        metadata = lock / "owner.json"
        metadata.write_bytes(json.dumps(owner).encode())
        metadata.chmod(0o600)
        return lock, metadata

    def test_kill_during_publication_leaves_a_recoverable_complete_lock(self):
        self.write(b"echo old\n")
        code = """import importlib.util, pathlib, sys
spec = importlib.util.spec_from_file_location('helper', sys.argv[1])
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)
original_write = helper.atomic_write
def paused_write(target, data, mode, **kwargs):
    original_write(target, data, mode, **kwargs)
    if target.name == 'owner.json':
        print('PUBLISHED', flush=True)
        sys.stdin.readline()
helper.atomic_write = paused_write
with helper.history_lock(pathlib.Path(sys.argv[2])):
    pass
"""
        with subprocess.Popen(
            [sys.executable, "-c", code, str(HELPER), str(self.history)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=self.env,
        ) as child:
            try:
                self.assertTrue(select.select([child.stdout], [], [], 10)[0])
                self.assertEqual(child.stdout.readline(), "PUBLISHED\n")
                lock = Path(str(self.history) + ".LOCK")
                self.assertEqual(json.loads((lock / "owner.json").read_bytes())["pid"], child.pid)
                self.assertEqual(lock.stat().st_mode & 0o777, 0o700)
            finally:
                child.kill()
                child.communicate(timeout=10)
        self.assertEqual(child.returncode, -9)
        result = self.run_helper("replace", "old", "new")
        self.assertEqual(result.returncode, 10, result.stderr)
        self.assertFalse(lock.exists())
        self.assertEqual(self.history.read_bytes(), b"echo new\n")

    def test_owner_masking_umask_keeps_new_lock_artifacts_usable(self):
        self.write(b"echo old\n")
        code = """import importlib.util, os, pathlib, sys
spec = importlib.util.spec_from_file_location('helper', sys.argv[1])
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)
target = pathlib.Path(sys.argv[2])
mask = int(sys.argv[3], 8)
os.umask(mask)
for _ in range(2):
    with helper.history_lock(target):
        lock = pathlib.Path(str(target) + '.LOCK')
        guard = pathlib.Path(str(target) + '.histfix-lock')
        assert guard.stat().st_mode & 0o777 == 0o600
        assert lock.stat().st_mode & 0o777 == 0o700
        assert (lock / 'owner.json').stat().st_mode & 0o777 == 0o600
        assert os.umask(mask) == mask
    assert os.umask(mask) == mask
"""
        guard = Path(str(self.history) + ".histfix-lock")
        for mask in ("277", "777"):
            with self.subTest(mask=mask):
                if guard.exists():
                    guard.unlink()
                result = subprocess.run([sys.executable, "-c", code, str(HELPER),
                                         str(self.history), mask],
                                        text=True, capture_output=True, env=self.env, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.history.read_bytes(), b"echo old\n")

    def test_masked_owner_kill_recovers_with_masked_reacquisition(self):
        self.write(b"echo old\n")
        pending = self.home / "pending"
        pending.write_bytes(b"echo later\n")
        with self.child_history_lock(mask=0o777) as child:
            child.kill()
            child.wait(timeout=10)
        code = """import os, runpy, sys
os.umask(0o777)
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name='__main__')
"""
        for _ in range(2):
            result = subprocess.run([sys.executable, "-c", code, str(HELPER),
                                     "--flush", str(pending)],
                                    text=True, capture_output=True, env=self.env, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(Path(str(self.history) + ".LOCK").exists())
        self.assertEqual(self.history.read_bytes(), b"echo old\necho later\necho later\n")
        self.assertEqual(Path(str(self.history) + ".histfix-lock").stat().st_mode & 0o777, 0o600)

    def test_creation_failures_restore_umask_without_chmodding_existing_guard(self):
        helper = self.load_helper()
        self.write(b"echo old\n")
        guard = Path(str(self.history) + ".histfix-lock")
        previous = os.umask(0o777)
        try:
            with mock.patch.object(helper.os, "open", side_effect=PermissionError):
                with self.assertRaises(PermissionError):
                    with helper.history_lock(self.history):
                        self.fail("entered after guard creation failure")
            self.assertEqual(os.umask(0o777), 0o777)
            with mock.patch.object(helper.Path, "mkdir", side_effect=PermissionError):
                with self.assertRaises(PermissionError):
                    with helper.history_lock(self.history):
                        self.fail("entered after directory creation failure")
            self.assertEqual(os.umask(0o777), 0o777)
            guard.chmod(0o400)
            before = guard.stat()
            with self.assertRaises(PermissionError):
                with helper.history_lock(self.history):
                    self.fail("entered with existing read-only guard")
            self.assertEqual((guard.stat().st_ino, guard.stat().st_mode),
                             (before.st_ino, before.st_mode))
            self.assertEqual(os.umask(0o777), 0o777)
        finally:
            os.umask(previous)

    def test_normal_lock_publishes_owner_and_keeps_guard_inode(self):
        self.write(b"echo old\n")
        helper = self.load_helper()
        lock = Path(str(self.history) + ".LOCK")
        guard = Path(str(self.history) + ".histfix-lock")
        for _ in range(2):
            with helper.history_lock(self.history):
                owner = json.loads((lock / "owner.json").read_bytes())
                self.assertEqual(owner["format"], "histfix-lock-v2")
                self.assertEqual(owner["pid"], os.getpid())
                self.assertEqual(owner["start"], helper.process_start(os.getpid()))
                self.assertTrue(owner["host"] and owner["boot"])
                current_inode = guard.stat().st_ino
            self.assertFalse(lock.exists())
            self.assertEqual(guard.stat().st_ino, current_inode)
        self.assertFalse(list(self.home.glob(".histfix-lock-*")))
        self.assertEqual(self.history.read_bytes(), b"echo old\n")

    def test_live_owner_and_old_lock_are_preserved(self):
        self.write(b"echo old\n")
        helper = self.load_helper()
        lock, metadata = self.write_owner_lock(helper.lock_owner())
        owner = metadata.read_bytes()
        os.utime(lock, (0, 0))  # Age must never override a live owner.
        result = self.run_helper("replace", "old", "new")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(metadata.read_bytes(), owner)
        self.assertEqual(self.history.read_bytes(), b"echo old\n")
        metadata.unlink()
        lock.rmdir()
        with self.child_history_lock():
            before = metadata.read_bytes()
            result = self.run_helper("replace", "old", "new")
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(metadata.read_bytes(), before)

    def test_unknown_legacy_and_zsh_regular_locks_are_preserved(self):
        self.write(b"echo old\n")
        helper = self.load_helper()
        lock = Path(str(self.history) + ".LOCK")
        current = helper.lock_owner()
        cases = [json.dumps(current).encode(), json.dumps(dict(current, format="histfix-lock-v1")).encode(), b"", b"12345\n", b"12345 host-fixture\n", b"/pid-12345/host-fixture\n", b"{", b"{}",
                 b"null", b"[]", b"x" * 4097]
        for change in (
            dict(format="histfix-lock-v3"), dict(host="foreign-host"),
            dict(namespace="foreign-namespace"), dict(start=None), dict(start="unknown"),
            dict(boot="unknown"), dict(boot="0" * 36), dict(pid=0), dict(pid=-1), dict(pid=True),
            dict(uid=os.getuid() + 1), dict(token="unknown"), dict(extra="unknown"),
        ):
            cases.append(json.dumps(dict(current, **change)).encode())
        missing = dict(current)
        del missing["token"]
        cases.append(json.dumps(missing).encode())
        for content in cases:
            with self.subTest(content=content[:150]):
                lock.write_bytes(content)
                lock.chmod(0o600)
                os.utime(lock, (0, 0))
                before = lock.stat()
                result = self.run_helper("replace", "old", "new")
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(lock.read_bytes(), content)
                self.assertEqual(lock.stat().st_ino, before.st_ino)
                self.assertEqual(self.history.read_bytes(), b"echo old\n")

    def test_zsh_hardlink_and_nonregular_locks_are_preserved(self):
        self.write(b"echo old\n")
        lock = Path(str(self.history) + ".LOCK")
        zsh_temporary = self.home / "zsh-lock-owner"
        zsh_temporary.write_bytes(b"12345 host-fixture\n")
        os.link(zsh_temporary, lock)
        result = self.run_helper("replace", "old", "new")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(lock.stat().st_ino, zsh_temporary.stat().st_ino)
        lock.unlink()
        for kind in ("directory", "fifo"):
            with self.subTest(kind=kind):
                if kind == "directory":
                    lock.mkdir()
                else:
                    os.mkfifo(lock)
                result = self.run_helper("replace", "old", "new")
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertTrue(lock.exists())
                if kind == "directory":
                    lock.rmdir()
                else:
                    lock.unlink()
        self.assertEqual(self.history.read_bytes(), b"echo old\n")

    def test_pid_reuse_and_previous_boot_recover_only_recognized_owners(self):
        self.write(b"echo old\n")
        helper = self.load_helper()
        for change in (dict(start="1:0" if sys.platform == "darwin" else "1"),
                       dict(boot="00000000-0000-0000-0000-000000000000")):
            with self.subTest(change=change):
                lock, _ = self.write_owner_lock(dict(helper.lock_owner(), **change))
                result = self.run_helper("replace", "old", "new")
                self.assertEqual(result.returncode, 10, result.stderr)
                self.assertFalse(lock.exists())
                self.assertEqual(self.history.read_bytes(), b"echo new\n")
                self.write(b"echo old\n")

    def test_previous_boot_recovers_recreated_linux_namespace_but_preserves_ambiguity(self):
        helper = self.load_helper()
        self.write(b"echo old\n")
        with mock.patch.object(helper.sys, "platform", "linux"), \
                mock.patch.object(helper, "system_identity", return_value=(
                    "a" * 32, "00000000-0000-0000-0000-000000000002")), \
                mock.patch.object(helper.os, "readlink", return_value="pid:[4026532000]"), \
                mock.patch.object(helper, "process_identity", return_value=("100", False)), \
                mock.patch.object(helper.os, "kill", side_effect=AssertionError("ambiguous PID probe")):
            current = helper.lock_owner()
            previous = "00000000-0000-0000-0000-000000000001"
            cases = ((dict(boot=previous, namespace="pid:[4026531999]"), True),
                     (dict(namespace="pid:[4026531999]"), False),
                     (dict(boot=previous, host="b" * 32), False),
                     (dict(boot=previous, namespace="unknown"), False))
            for changes, expected in cases:
                with self.subTest(changes=changes):
                    owner = dict(current, **changes)
                    lock, metadata = self.write_owner_lock(owner)
                    before = metadata.read_bytes()
                    try:
                        self.assertEqual(helper.abandoned_owner(owner, current), expected)
                        if expected:
                            with helper.history_lock(self.history):
                                recovered = json.loads(metadata.read_bytes())
                                self.assertEqual(recovered["boot"], current["boot"])
                                self.assertEqual(recovered["namespace"], current["namespace"])
                            self.assertFalse(lock.exists())
                        else:
                            with self.assertRaisesRegex(ValueError, "locked"):
                                with helper.history_lock(self.history):
                                    self.fail("entered with ambiguous owner")
                            self.assertEqual(metadata.read_bytes(), before)
                    finally:
                        if lock.exists():
                            metadata.unlink()
                            lock.rmdir()
            self.assertFalse(helper.abandoned_owner(
                dict(current, boot=previous), dict(current, namespace="unknown")))
        self.assertEqual(self.history.read_bytes(), b"echo old\n")

    def test_unavailable_identity_and_permission_probes_refuse_recovery(self):
        helper = self.load_helper()
        current = helper.lock_owner()
        # A reused PID cannot be established if start identity cannot be read.
        old = dict(current, start="1:0" if sys.platform == "darwin" else "1")
        with mock.patch.object(helper, "process_identity", return_value=(None, None)):
            self.assertFalse(helper.abandoned_owner(old, current))
        with mock.patch.object(helper.os, "kill", side_effect=PermissionError):
            self.assertFalse(helper.abandoned_owner(old, current))
        for key in ("host", "boot", "start", "namespace"):
            with self.subTest(key=key):
                self.assertFalse(helper.abandoned_owner(old, dict(current, **{key: None})))
        self.write(b"echo old\n")
        lock, metadata = self.write_owner_lock(old)
        before = metadata.read_bytes()
        with mock.patch.object(helper, "system_identity", return_value=(None, None)):
            with self.assertRaisesRegex(ValueError, "locked"):
                with helper.history_lock(self.history):
                    self.fail("entered without host identity")
        self.assertEqual(metadata.read_bytes(), before)

    def test_cleanup_does_not_remove_a_replaced_lock(self):
        self.write(b"echo old\n")
        helper = self.load_helper()
        lock = Path(str(self.history) + ".LOCK")
        with helper.history_lock(self.history):
            lock.rename(self.home / "retained-lock")
            lock.symlink_to("/pid-12345/host-fixture")
        self.assertTrue(lock.is_symlink())
        self.assertEqual(os.readlink(lock), "/pid-12345/host-fixture")

    def test_recovery_does_not_remove_a_lock_changed_during_inspection(self):
        self.write(b"echo old\n")
        helper = self.load_helper()
        lock, _ = self.write_owner_lock(helper.lock_owner())

        def replace_lock(owner, current):
            lock.rename(self.home / "retained-lock")
            lock.symlink_to("/pid-12345/host-fixture")
            return True

        with mock.patch.object(helper, "abandoned_owner", side_effect=replace_lock):
            with self.assertRaisesRegex(ValueError, "locked"):
                with helper.history_lock(self.history):
                    self.fail("entered after lock changed")
        self.assertTrue(lock.is_symlink())

    def test_in_place_owner_change_and_unsafe_permissions_are_preserved(self):
        self.write(b"echo old\n")
        helper = self.load_helper()
        lock, metadata = self.write_owner_lock(helper.lock_owner())

        def change_owner(owner, current):
            metadata.write_bytes(b"unknown replacement owner\n")
            return True

        with mock.patch.object(helper, "abandoned_owner", side_effect=change_owner):
            with self.assertRaisesRegex(ValueError, "locked"):
                with helper.history_lock(self.history):
                    self.fail("entered after in-place owner change")
        self.assertEqual(metadata.read_bytes(), b"unknown replacement owner\n")
        metadata.write_text(json.dumps(dict(helper.lock_owner(), boot="00000000-0000-0000-0000-000000000000")))
        original_open = helper.os.open

        def changed_permissions(path, flags, *args, **kwargs):
            if path == "owner.json":
                metadata.chmod(0o666)
            return original_open(path, flags, *args, **kwargs)

        with mock.patch.object(helper.os, "open", side_effect=changed_permissions):
            with self.assertRaisesRegex(ValueError, "locked"):
                with helper.history_lock(self.history):
                    self.fail("entered after owner permissions changed before open")
        self.assertEqual(self.run_helper("replace", "old", "new").returncode, 2)
        self.assertEqual(metadata.stat().st_mode & 0o777, 0o666)
        self.assertEqual(self.history.read_bytes(), b"echo old\n")

    def test_linux_identity_probes_parse_kernel_fixtures(self):
        helper = self.load_helper()
        # The command field may contain spaces and closing parentheses.
        fixture = "123 (command with ) parentheses) S " + "0 " * 18 + "9876 0"
        with mock.patch.object(helper.sys, "platform", "linux"), \
                mock.patch.object(helper.Path, "read_text", return_value=fixture):
            self.assertEqual(helper.process_start(123), "9876")
        with mock.patch.object(helper.sys, "platform", "linux"), \
                mock.patch.object(helper.Path, "read_text", side_effect=[
                    "a" * 32, "00000000-0000-0000-0000-000000000001\n"]):
            self.assertEqual(helper.system_identity(),
                             ("a" * 32, "00000000-0000-0000-0000-000000000001"))
        with mock.patch.object(helper.sys, "platform", "linux"), \
                mock.patch.object(helper.Path, "read_text", side_effect=PermissionError):
            self.assertIsNone(helper.process_start(123))
            self.assertEqual(helper.system_identity(), (None, None))

    def test_process_state_and_start_identity_share_kernel_snapshot(self):
        helper = self.load_helper()
        for state in ("S", "Z"):
            fixture = f"123 (command with ) parentheses) {state} " + "0 " * 18 + "9876 0"
            with self.subTest(platform="linux", state=state), \
                    mock.patch.object(helper.sys, "platform", "linux"), \
                    mock.patch.object(helper.Path, "read_text", return_value=fixture):
                self.assertEqual(helper.process_identity(123), ("9876", state == "Z"))
        for status in (2, 4, 5):  # SRUN, SSTOP, SZOMB in the Darwin SDK
            libc = mock.Mock()

            def probe(pid, flavor, argument, buffer, size):
                self.assertEqual((flavor, argument), (3, 1))
                values = [0] * 12 + [b"" , b""] + [0] * 6 + [100, 1]
                values[1], values[3] = status, pid
                buffer.raw = helper.struct.pack("=12I16s32s6I2Q", *values)
                return size

            libc.proc_pidinfo.side_effect = probe
            with self.subTest(platform="darwin", status=status), \
                    mock.patch.object(helper.sys, "platform", "darwin"), \
                    mock.patch.object(helper.ctypes, "CDLL", return_value=libc):
                self.assertEqual(helper.process_identity(123), ("100:1", status == 5))
        current = helper.lock_owner()
        with mock.patch.object(helper, "process_identity", return_value=(None, True)):
            self.assertFalse(helper.abandoned_owner(current, current))
        with mock.patch.object(helper, "process_identity", return_value=(current["start"], False)):
            self.assertFalse(helper.abandoned_owner(current, current))
        with mock.patch.object(helper, "process_identity", return_value=(current["start"], True)):
            self.assertTrue(helper.abandoned_owner(current, current))

    def test_macos_identity_uses_native_apis_with_bounded_readonly_probes(self):
        helper = self.load_helper()
        libc = mock.Mock()

        def get_host(buffer, wait):
            self.assertEqual((wait._obj.seconds, wait._obj.nanoseconds), (5, 0))
            buffer.raw = bytes.fromhex("11" * 16)
            return 0

        def get_boot(name, buffer, length, new_value, new_length):
            self.assertEqual(name, b"kern.bootsessionuuid")
            self.assertEqual(length._obj.value, 37)
            self.assertIsNone(new_value)
            self.assertEqual(new_length, 0)
            buffer.raw = b"00000000-0000-0000-0000-000000000001\0"
            return 0

        libc.gethostuuid.side_effect = get_host
        libc.sysctlbyname.side_effect = get_boot
        with mock.patch.object(helper.sys, "platform", "darwin"), \
                mock.patch.object(helper.ctypes, "CDLL", return_value=libc), \
                mock.patch.object(subprocess, "Popen", side_effect=AssertionError("external command")):
            self.assertEqual(helper.system_identity(),
                             ("11111111-1111-1111-1111-111111111111",
                              "00000000-0000-0000-0000-000000000001"))

    def test_macos_native_identity_failures_disable_recovery(self):
        helper = self.load_helper()
        for failure in ("host-error", "empty-host", "boot-error", "short", "non-ascii", "no-nul"):
            with self.subTest(failure=failure):
                libc = mock.Mock()

                def get_host(buffer, wait):
                    if failure != "empty-host":
                        buffer.raw = bytes.fromhex("11" * 16)
                    return -1 if failure == "host-error" else 0

                def get_boot(name, buffer, length, new_value, new_length):
                    buffer.raw = b"00000000-0000-0000-0000-000000000001\0"
                    if failure == "short":
                        length._obj.value = 36
                    elif failure == "non-ascii":
                        buffer.raw = b"\xff" * 36 + b"\0"
                    elif failure == "no-nul":
                        buffer.raw = b"a" * 37
                    return -1 if failure == "boot-error" else 0

                libc.gethostuuid.side_effect = get_host
                libc.sysctlbyname.side_effect = get_boot
                with mock.patch.object(helper.sys, "platform", "darwin"), \
                        mock.patch.object(helper.ctypes, "CDLL", return_value=libc):
                    self.assertEqual(helper.system_identity(), (None, None))
        with mock.patch.object(helper.sys, "platform", "darwin"), \
                mock.patch.object(helper.ctypes, "CDLL", side_effect=OSError):
            self.assertEqual(helper.system_identity(), (None, None))
        with mock.patch.object(helper.sys, "platform", "darwin"), \
                mock.patch.object(helper.ctypes, "CDLL", return_value=object()):
            self.assertEqual(helper.system_identity(), (None, None))

    @unittest.skipUnless(sys.platform == "darwin", "native macOS identity")
    def test_native_macos_identity_never_starts_external_commands(self):
        helper = self.load_helper()
        with mock.patch.object(subprocess, "Popen", side_effect=AssertionError("external command")):
            first = helper.system_identity()
            self.assertTrue(all(first), "native machine and boot identity unavailable")
            self.assertEqual(helper.system_identity(), first)

    def test_unrecognized_recovery_guards_are_preserved(self):
        self.write(b"echo old\n")
        guard = Path(str(self.history) + ".histfix-lock")
        referent = self.home / "unrelated"
        referent.write_bytes(b"do not change\n")
        guard.symlink_to(referent)
        self.assertEqual(self.run_helper("replace", "old", "new").returncode, 2)
        self.assertEqual(referent.read_bytes(), b"do not change\n")
        guard.unlink()
        guard.write_bytes(b"")
        guard.chmod(0o666)
        self.assertEqual(self.run_helper("replace", "old", "new").returncode, 2)
        self.assertEqual(guard.stat().st_mode & 0o777, 0o666)
        self.assertEqual(self.history.read_bytes(), b"echo old\n")

    def test_two_processes_recover_without_overlapping_critical_sections(self):
        self.write(b"echo old\n")
        code = """import importlib.util, pathlib, sys
spec = importlib.util.spec_from_file_location('helper', sys.argv[1])
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)
target = pathlib.Path(sys.argv[2])
inside = target.parent / 'inside'
original_probe = helper.abandoned_owner
def paused_probe(owner, current):
    abandoned = original_probe(owner, current)
    if abandoned:
        print('OBSERVED', flush=True)
        sys.stdin.readline()
    return abandoned
helper.abandoned_owner = paused_probe
print('READY', flush=True)
sys.stdin.readline()
try:
    with helper.history_lock(target):
        with inside.open('x'):
            print('ENTERED', flush=True)
            sys.stdin.readline()
        inside.unlink()
except ValueError:
    print('BLOCKED', flush=True)
"""
        for _ in range(8):
            with self.child_history_lock() as owner:
                owner.kill()
                owner.wait(timeout=10)
            children = []
            try:
                for _ in range(2):
                    child = subprocess.Popen(
                        [sys.executable, "-c", code, str(HELPER), str(self.history)],
                        text=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, env=self.env,
                    )
                    children.append(child)
                    self.assertTrue(select.select([child.stdout], [], [], 10)[0])
                    self.assertEqual(child.stdout.readline(), "READY\n")
                for child in children:
                    child.stdin.write("go\n")
                    child.stdin.flush()
                outcomes = []
                for child in children:
                    self.assertTrue(select.select([child.stdout], [], [], 10)[0])
                    outcomes.append(child.stdout.readline().strip())
                # Pause after proving abandonment, before unlink: the other
                # process must be blocked from observing the same stale owner.
                self.assertCountEqual(outcomes, ["OBSERVED", "BLOCKED"])
                winner = children[outcomes.index("OBSERVED")]
                winner.stdin.write("recover\n")
                winner.stdin.flush()
                self.assertTrue(select.select([winner.stdout], [], [], 10)[0])
                self.assertEqual(winner.stdout.readline(), "ENTERED\n")
                lock = Path(str(self.history) + ".LOCK")
                self.assertEqual(json.loads((lock / "owner.json").read_bytes())["pid"], winner.pid)
                for child, outcome in zip(children, outcomes):
                    _, error = child.communicate("release\n" if outcome == "OBSERVED" else None,
                                                 timeout=10)
                    self.assertEqual(child.returncode, 0, error)
                self.assertFalse(lock.exists())
                self.assertEqual(self.history.read_bytes(), b"echo old\n")
            finally:
                for child in children:
                    if child.poll() is None:
                        child.kill()
                    child.communicate(timeout=10)

    def test_live_zsh_fcntl_owner_blocks_histfix(self):
        self.write(b"echo old\n")
        helper = self.load_helper()
        # Exercise zsh's actual zsystem fcntl primitive in an isolated shell.
        code = '''zmodload zsh/system || exit 90
zsystem flock -f held "$1" || exit 91
print -r -- ACQUIRED
read -r reply
'''
        with subprocess.Popen(
            ["zsh", "-f", "-c", code, "fixture", str(self.history)],
            text=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=dict(self.env, HOME=str(self.home), ZDOTDIR=str(self.home)),
        ) as child:
            try:
                self.assertTrue(select.select([child.stdout], [], [], 10)[0])
                self.assertEqual(child.stdout.readline(), "ACQUIRED\n")
                with self.assertRaises(BlockingIOError):
                    with helper.history_lock(self.history):
                        self.fail("entered while zsh held its fcntl lock")
                self.assertFalse(Path(str(self.history) + ".LOCK").exists())
                self.assertEqual(self.history.read_bytes(), b"echo old\n")
            finally:
                child.communicate("release\n", timeout=10)

    def test_aged_directory_lock_blocks_native_zsh_without_losing_history(self):
        self.write(b"echo old\n")
        lock = Path(str(self.history) + ".LOCK")
        child = None
        try:
            with self.child_history_lock():
                os.utime(lock, (time.time() - 30, time.time() - 30))
                code = '''HISTFILE=$1; HISTSIZE=100; SAVEHIST=100
unsetopt HIST_FCNTL_LOCK
print -s -- 'echo zsh pending'
print -r -- READY
fc -A "$HISTFILE"
print -r -- DONE
HISTFILE=''
'''
                child = subprocess.Popen(
                    ["zsh", "-f", "-i", "-c", code, "fixture", str(self.history)],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, env=dict(self.env, HOME=str(self.home), ZDOTDIR=str(self.home)),
                )
                self.assertTrue(select.select([child.stdout], [], [], 10)[0])
                self.assertEqual(child.stdout.readline(), "READY\n")
                with self.assertRaises(subprocess.TimeoutExpired,
                                       msg="zsh entered while histfix held its aged lock"):
                    child.wait(timeout=0.5)
                self.assertEqual(self.history.read_bytes(), b"echo old\n")
            output, error = child.communicate(timeout=10)
            self.assertEqual(child.returncode, 0, error)
            self.assertIn("DONE", output)
            self.assertEqual(self.history.read_bytes(), b"echo old\necho zsh pending\n")
        finally:
            if child is not None:
                if child.poll() is None:
                    child.kill()
                child.communicate(timeout=10)

    def test_zsh_winning_recovery_gap_is_preserved(self):
        self.write(b"echo old\n")
        helper = self.load_helper()
        lock, _ = self.write_owner_lock(dict(helper.lock_owner(),
                                             boot="00000000-0000-0000-0000-000000000000"))
        original_recover = helper.recover_history_lock

        def zsh_wins_gap(path, owner):
            recovered = original_recover(path, owner)
            self.assertTrue(recovered)
            lock.symlink_to("/pid-12345/host-fixture")
            return recovered

        with mock.patch.object(helper, "recover_history_lock", side_effect=zsh_wins_gap):
            with self.assertRaisesRegex(ValueError, "locked"):
                with helper.history_lock(self.history):
                    self.fail("entered despite a new zsh lock")
        self.assertTrue(lock.is_symlink())
        self.assertEqual(self.history.read_bytes(), b"echo old\n")

    def test_unknown_directory_owners_and_extra_entries_are_preserved(self):
        self.write(b"echo old\n")
        helper = self.load_helper()
        current = helper.lock_owner()
        lock, metadata = self.write_owner_lock(current)
        cases = [b"", b"{", b"null", b"[]", b"{}", b"x" * 4097]
        for change in (
            dict(format="histfix-lock-v1"), dict(format="histfix-lock-v3"),
            dict(host="foreign-host"), dict(namespace="foreign-namespace"),
            dict(start=None), dict(start="unknown"), dict(boot=None), dict(boot="0" * 36),
            dict(pid=0), dict(pid=-1), dict(pid=True), dict(uid=os.getuid() + 1),
            dict(token="unknown"), dict(extra="unknown"),
        ):
            cases.append(json.dumps(dict(current, **change)).encode())
        for data in cases:
            with self.subTest(data=data[:100]):
                metadata.write_bytes(data)
                result = self.run_helper("replace", "old", "new")
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(metadata.read_bytes(), data)
                self.assertEqual(self.history.read_bytes(), b"echo old\n")
        metadata.write_text(json.dumps(dict(current, boot="00000000-0000-0000-0000-000000000000")))
        before = metadata.read_bytes()
        extra = lock / "unrelated"
        extra.write_bytes(b"retain this\n")
        self.assertEqual(self.run_helper("replace", "old", "new").returncode, 2)
        self.assertEqual(metadata.read_bytes(), before)
        self.assertEqual(extra.read_bytes(), b"retain this\n")
        extra.unlink()
        lock.chmod(0o755)
        self.assertEqual(self.run_helper("replace", "old", "new").returncode, 2)
        self.assertEqual(metadata.read_bytes(), before)

    def test_unknown_owner_file_types_and_incomplete_directories_are_preserved(self):
        self.write(b"echo old\n")
        lock = Path(str(self.history) + ".LOCK")
        lock.mkdir(mode=0o700)
        metadata = lock / "owner.json"
        referent = self.home / "unrelated-owner"
        referent.write_bytes(b"do not change\n")
        for kind in ("missing", "symlink", "hardlink", "fifo", "directory"):
            with self.subTest(kind=kind):
                if kind == "symlink":
                    metadata.symlink_to(referent)
                elif kind == "hardlink":
                    os.link(referent, metadata)
                elif kind == "fifo":
                    os.mkfifo(metadata, 0o600)
                elif kind == "directory":
                    metadata.mkdir()
                result = self.run_helper("replace", "old", "new")
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertTrue(lock.is_dir())
                self.assertEqual(referent.read_bytes(), b"do not change\n")
                if kind == "directory":
                    metadata.rmdir()
                elif kind != "missing":
                    metadata.unlink()
        self.assertEqual(self.history.read_bytes(), b"echo old\n")

    def test_lock_exceptions_release_directory_and_guard(self):
        self.write(b"echo old\n")
        helper = self.load_helper()
        lock = Path(str(self.history) + ".LOCK")
        with self.assertRaisesRegex(RuntimeError, "fixture"):
            with helper.history_lock(self.history):
                raise RuntimeError("fixture")
        self.assertFalse(lock.exists())
        original_write = helper.atomic_write

        def fail_publication(target, data, mode, **kwargs):
            if target.name == "owner.json":
                raise OSError("publication fixture")
            return original_write(target, data, mode, **kwargs)

        with mock.patch.object(helper, "atomic_write", side_effect=fail_publication):
            with self.assertRaisesRegex(OSError, "publication fixture"):
                with helper.history_lock(self.history):
                    self.fail("entered despite publication failure")
        self.assertFalse(lock.exists())
        with helper.history_lock(self.history):
            self.assertTrue(lock.is_dir())
        self.assertFalse(lock.exists())

    def test_directory_open_and_fstat_failures_release_guard_and_fd(self):
        self.write(b"echo old\n")
        helper = self.load_helper()
        lock = Path(str(self.history) + ".LOCK")
        original_open = helper.os.open
        original_fstat = helper.os.fstat
        for fail_at in ("open", "fstat"):
            directory_fds = []

            def failed_open(path, flags, *args, **kwargs):
                if path == lock and fail_at == "open":
                    raise OSError("open fixture")
                fd = original_open(path, flags, *args, **kwargs)
                if path == lock:
                    directory_fds.append(fd)
                return fd

            def failed_fstat(fd):
                if fd in directory_fds:
                    raise OSError("fstat fixture")
                return original_fstat(fd)

            with mock.patch.object(helper.os, "open", side_effect=failed_open), \
                    mock.patch.object(helper.os, "fstat", side_effect=failed_fstat):
                with self.assertRaisesRegex(OSError, fail_at + " fixture"):
                    with helper.history_lock(self.history):
                        self.fail("entered despite descriptor failure")
            self.assertFalse(lock.exists())
            for fd in directory_fds:
                with self.assertRaises(OSError):
                    original_fstat(fd)
            with helper.history_lock(self.history):
                self.assertTrue(lock.is_dir())
            self.assertFalse(lock.exists())

    def test_lock_cleanup_failure_keeps_applied_result(self):
        self.write(b"echo old\n")
        helper = self.load_helper()
        lock = Path(str(self.history) + ".LOCK")
        original_rmdir = helper.os.rmdir

        def fail_lock_cleanup(path, *args, **kwargs):
            if Path(path).resolve() == lock.resolve():
                raise OSError("cleanup fixture")
            return original_rmdir(path, *args, **kwargs)

        error = io.StringIO()
        with mock.patch.dict(os.environ, HISTFIX_FILE=str(self.history)), \
                mock.patch.object(helper, "confirm", return_value=True), \
                mock.patch.object(helper.os, "rmdir", side_effect=fail_lock_cleanup), \
                mock.patch.object(helper.sys, "stderr", error):
            self.assertEqual(helper.main(["replace", "old", "new"]), 10)
        self.assertEqual(self.history.read_bytes(), b"echo new\n")
        self.assertIn("could not remove lock directory", error.getvalue())
        self.assertEqual(list(lock.iterdir()), [])
        lock.rmdir()  # Manual cleanup is safe here: all fixture writers are stopped.
        self.assertEqual(self.run_helper("undo").returncode, 10)

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

    def test_completion_registers_before_or_after_compinit_without_running_it(self):
        # A copy without _histfix matches a package that installs only the plugin.
        bare = self.home / "bare"
        bare.mkdir()
        for name in ("histfix.plugin.zsh", "histfix.py"):
            (bare / name).write_bytes((ROOT / name).read_bytes())
        cases = {
            "compinit before source": '''
autoload -Uz compinit && compinit -u -D || exit 90
source "$2" || exit 91
[[ $_comps[histfix] == _histfix ]] || exit 92
''',
            "compinit after source": '''
source "$2" || exit 91
[[ $fpath[1] == ${2:h} ]] || exit 92
autoload -Uz compinit && compinit -u -D || exit 90
[[ $_comps[histfix] == _histfix ]] || exit 93
''',
            "no compinit": '''
source "$2" || exit 91
(( ! $+functions[compdef] && ! $+_comps )) || exit 92
''',
            "no completion file": '''
autoload -Uz compinit && compinit -u -D || exit 90
saved=($fpath)
source "$3/histfix.plugin.zsh" || exit 91
(( ! $+_comps[histfix] )) || exit 92
[[ "$fpath" == "$saved" ]] || exit 93
''',
        }
        for name, script in cases.items():
            with self.subTest(name):
                result = self.run_zsh(script, str(bare))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr, "")

    def test_completion_completes_commands_and_options_at_a_prompt(self):
        master, slave = pty.openpty()
        process = subprocess.Popen(
            ["zsh", "-f", "-i"], stdin=slave, stdout=slave, stderr=slave,
            start_new_session=True,
            env=dict(self.env, HOME=str(self.home), ZDOTDIR=str(self.home), TERM="dumb"),
        )
        os.close(slave)
        output = b""

        def until(marker):
            nonlocal output
            deadline = time.monotonic() + 10
            while marker not in output:
                if time.monotonic() >= deadline:
                    self.fail(f"zsh did not emit {marker!r}: {output!r}")
                if select.select([master], [], [], 0.1)[0]:
                    output += os.read(master, 65536)

        try:
            # Ctrl-B runs the edited line as a print of the buffer between angle
            # brackets, so the marker appears only after completion has finished.
            os.write(master, (
                f"autoload -Uz compinit; compinit -u -D; source {shlex.quote(str(PLUGIN))}; "
                "histfix-test-show() { BUFFER=\"print -r -- '<'${(q)BUFFER}'>'\"; "
                "zle accept-line }; zle -N histfix-test-show; "
                "bindkey '^B' histfix-test-show; PROMPT='HF''> '\n").encode())
            until(b"HF> ")
            for typed, completed in (
                ("histfix rep", "histfix replace "),
                ("histfix replace --ig", "histfix replace --ignore-case "),
                ("histfix undo --dr", "histfix undo --dry-run "),
                ("histfix replace --regex fo", "histfix replace --regex fo"),
                ("histfix replace -- --x", "histfix replace -- --x"),
            ):
                with self.subTest(typed):
                    output = b""
                    os.write(master, typed.encode() + b"\t\x02")
                    until(f"<{completed}>".encode())
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            os.close(master)

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

    def test_undo_backup_cleanup_failure_still_refreshes_shell_history(self):
        wrapper = self.home / "helper-with-unlink-failure.py"
        wrapper.write_text(f'''import runpy
from pathlib import Path
original_unlink = Path.unlink
def fail_backup_unlink(target, *args, **kwargs):
    if target.name.endswith(".histfix-undo.json"):
        raise OSError("injected backup deletion failure")
    return original_unlink(target, *args, **kwargs)
Path.unlink = fail_backup_unlink
runpy.run_path({str(HELPER)!r}, run_name="__main__")
''')
        self.write(b": 100:1;echo old\n")
        with self.interactive_zsh("setopt EXTENDED_HISTORY") as (send, until, process):
            send("histfix replace old new")
            until(b"[y/N] ")
            send("y")
            self.assertIn(b"History updated.", until(b"HF> "))
            send(f"_HISTFIX_HELPER={shlex.quote(str(wrapper))}")
            until(b"HF> ")
            send("histfix undo")
            until(b"[y/N] ")
            send("y")
            undo_output = until(b"HF> ")
            send('print -r -- "UNDO:$? MEMORY:${history[(r)echo*]}"')
            memory_output = until(b"HF> ")
            self.assertTrue(self.history.read_bytes().startswith(b": 100:1;echo old\n"))
            self.assertIn(b"UNDO:0 MEMORY:echo old\r\n", memory_output)
            self.assertIn(b"Replacement undone.", undo_output)
            self.assertIn(b"could not remove undo backup", undo_output)
            self.assertTrue(Path(str(self.history) + ".histfix-undo.json").exists())
            send("histfix undo")
            self.assertIn(b"history changed since replacement", until(b"HF> "))
            send("exit")
            process.wait(timeout=5)

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

    def test_share_history_preview_keeps_disk_and_memory(self):
        action = "histfix replace --dry-run old new"
        self.write(b": 100:1;echo old\n")
        with self.interactive_zsh("setopt EXTENDED_HISTORY SHARE_HISTORY") as (send, until, process):
            send('cp "$HISTFILE" "$HISTFILE.before-disk"; '
                 '( fc -W "$HISTFILE.before-memory" ); '
                 f'{action} <<< y; histfix_exit=$?; '
                 '( fc -W "$HISTFILE.after-memory" ); '
                 'print -r -- "CODE:$histfix_exit OPTION:$options[sharehistory]"')
            output = until(b"HF> ")
            self.assertIn(b"CODE:0 OPTION:on\r\n", output)
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

    def test_share_history_applies_and_undoes_without_duplicates(self):
        self.write(b": 100:1;echo token-old\n: 101:1;echo keep\n")
        listing = Path(str(self.history) + ".listing")
        with self.interactive_zsh("SAVEHIST=90; setopt EXTENDED_HISTORY SHARE_HISTORY") as (
                send, until, process):
            def events():
                # fc -l reads the list without writing a history file.
                send(f"fc -ln 1 >| {shlex.quote(str(listing))}")
                until(b"HF> ")
                return [line.strip() for line in listing.read_text().splitlines()
                        if not line.strip().startswith("fc -ln 1")]

            def assert_unique(listed):
                repeated = sorted({event for event in listed if listed.count(event) > 1})
                self.assertEqual(repeated, [], listed)

            for action, removed, kept in (
                ("histfix replace token-old token-new", "echo token-old", "echo token-new"),
                ("histfix undo", "echo token-new", "echo token-old"),
            ):
                with self.subTest(action=action):
                    send(f"{action} <<< y; "
                         'print -r -- "CODE:$? SIZES:$HISTSIZE/$SAVEHIST OPTION:$options[sharehistory]"')
                    self.assertIn(b"CODE:0 SIZES:100/90 OPTION:on\r\n", until(b"HF> "))
                    for step in range(3):
                        send(f"echo {action.split()[1]}-{step}")
                        until(b"HF> ")
                    listed = events()
                    self.assertIn(kept, listed)
                    self.assertNotIn(removed, listed)
                    assert_unique(listed)
                    # A later record from another writer is still imported at a prompt.
                    with self.history.open("ab") as stream:
                        stream.write(f": {int(time.time())}:0;echo external-{action.split()[1]}\n"
                                     .encode())
                    send(":")
                    until(b"HF> ")
                    self.assertIn(f"echo external-{action.split()[1]}", events())
            self.assertIn(b"echo token-old", self.history.read_bytes())
            self.assertNotIn(b"echo token-new", self.history.read_bytes())
            send('HISTFILE=""; exit')
            self.assertEqual(process.wait(timeout=5), 0)

    def test_share_history_exit_after_apply_keeps_the_replacement(self):
        # The exit-time save must not restore the history level below fc -p.
        # SAVEHIST=5 makes that save trim and rewrite the whole file.
        self.write(b": 100:1;echo first\n: 101:1;echo token-old\n")
        with self.interactive_zsh("SAVEHIST=5; setopt EXTENDED_HISTORY SHARE_HISTORY") as (
                send, until, process):
            send("histfix replace token-old token-new <<< y")
            self.assertIn(b"History updated.", until(b"HF> "))
            for step in range(2):
                send(f"echo step-{step}")
                until(b"HF> ")
            send("exit")
            self.assertEqual(process.wait(timeout=5), 0)
        saved = self.history.read_bytes()
        commands = [line.split(b";", 1)[1] for line in saved.splitlines()]
        self.assertNotIn(b"echo first", commands, saved)  # the exit save trimmed the file
        self.assertIn(b"echo token-new", commands)
        self.assertNotIn(b"echo token-old", commands)
        self.assertEqual(len(commands), len(set(commands)), saved)

    def test_validate_treats_options_after_double_dash_as_text(self):
        # The plugin refuses writes without history events unless validation says dry run.
        self.assertEqual(self.run_helper("--validate", "replace", "--", "old", "--dry-run").returncode, 11)
        self.assertEqual(self.run_helper("--validate", "replace", "--dry-run", "old", "new").returncode, 12)


if __name__ == "__main__":
    unittest.main(verbosity=2)
