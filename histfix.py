"""Replace text in zsh history records without evaluating shell commands."""

import argparse
import base64
from collections import defaultdict
from contextlib import contextmanager
import ctypes
import fcntl
import io
import json
import os
from pathlib import Path
import re
import stat
import struct
import subprocess
import sys
import tempfile
import uuid


APPLIED = 10
VALIDATED = 11
DRY_RUN_VALIDATED = 12
CAPTURE = re.compile(r"\$(\$|&|[0-9]{1,2})")
SELF_COMMAND = re.compile(r"^\s*(?:noglob\s+)?histfix(?:\s|$)")


def arguments(argv):
    parser = argparse.ArgumentParser(prog="histfix", allow_abbrev=False)
    commands = parser.add_subparsers(dest="command", required=True)
    replace = commands.add_parser("replace", help="preview and replace history text",
                                  allow_abbrev=False)
    replace.add_argument("--regex", action="store_true", help="use Python regex syntax")
    replace.add_argument("--ignore-case", action="store_true")
    replace.add_argument("--dry-run", action="store_true", help="preview without applying")
    replace.add_argument("find")
    replace.add_argument("replacement")
    undo = commands.add_parser("undo", help="undo the last replacement",
                               allow_abbrev=False)
    undo.add_argument("--dry-run", action="store_true")
    if not argv or argv == ["help"]:
        parser.print_help()
        return None
    args = parser.parse_args(argv)
    if args.command == "replace":
        if not args.find:
            raise ValueError("find text must not be empty")
        if "\0" in args.find or "\0" in args.replacement:
            raise ValueError("NUL bytes are not supported")
        args.pattern = re.compile(args.find if args.regex else re.escape(args.find),
                                  re.IGNORECASE if args.ignore_case else 0)
        if args.regex:
            for token in CAPTURE.finditer(args.replacement):
                value = token[1]
                if value.isdigit() and int(value) > args.pattern.groups:
                    raise ValueError(f"replacement refers to missing capture ${value}")
    return args


def replacement(match, template):
    def expand(token):
        value = token[1]
        if value == "$":
            return "$"
        if value == "&":
            return match[0]
        return match[int(value)] or ""

    return CAPTURE.sub(expand, template)


def unmeta(data):
    decoded = bytearray()
    index = 0
    while index < len(data):
        value = data[index]
        if value == 0x83:
            index += 1
            if index == len(data):
                raise ValueError("history ends in an incomplete zsh Meta escape")
            value = data[index] ^ 32
        decoded.append(value)
        index += 1
    return decoded.decode("utf-8", errors="surrogateescape")


def metafy(text):
    encoded = bytearray()
    for value in text.encode("utf-8", errors="surrogateescape"):
        # zsh 5.9's Meta..Marker token range (Src/zsh.h).
        if value == 0 or 0x83 <= value <= 0xA2:
            encoded.extend((0x83, value ^ 32))
        else:
            encoded.append(value)
    return bytes(encoded)


def records(data):
    """Read zsh's escaped physical lines, retaining untouched records verbatim."""
    if b"\0" in data:
        raise ValueError("history contains a NUL byte")
    physical = []
    logical = []
    for line in io.BytesIO(data):
        physical.append(line)
        if line.endswith(b"\\\n"):
            logical.append(line[:-2] + b"\n")
            continue
        body = line[:-1] if line.endswith(b"\n") else line
        # zsh appends one padding space after a trailing backslash plus spaces.
        if line.endswith(b"\n") and re.search(rb"\\ +$", body):
            body = body[:-1]
        logical.append(body)
        content = b"".join(logical)
        header = re.match(rb": -?[0-9]+:-?[0-9]+;", content)
        prefix = header[0] if header else b""
        command = content[len(prefix):]
        if not prefix and command.startswith(b"\\:"):
            command = command[1:]
        yield b"".join(physical), prefix, unmeta(command)
        physical, logical = [], []
    if physical:
        raise ValueError("history ends in an incomplete multiline entry")


def encode_record(prefix, command):
    body = metafy(command)
    if not prefix and body.startswith(b":"):
        body = b"\\" + body
    body = body.replace(b"\n", b"\\\n")
    if re.search(rb"\\ *\Z", body):
        body += b" "
    return prefix + body + b"\n"


def display_command(command):
    quoted = json.dumps(command, ensure_ascii=False)
    return "".join(character if character.isprintable() else f"\\u{ord(character):04x}"
                   for character in quoted)


def preview(before, args):
    output = []
    changes = []
    for number, (raw, prefix, command) in enumerate(records(before), 1):
        if SELF_COMMAND.match(command):
            output.append(raw)
            continue
        changed = args.pattern.sub(
            lambda match: replacement(match, args.replacement) if args.regex
            else args.replacement, command,
        )
        if changed == command:
            output.append(raw)
            continue
        if not changed.strip():
            raise ValueError("replacement would create an empty history entry")
        changes.append((number, command, changed))
        output.append(encode_record(prefix, changed))
    for number, old, new in changes:
        # Escape non-printable Unicode too: JSON alone permits C1 controls.
        print(f"{number} - {display_command(old)}")
        print(f"{number} + {display_command(new)}")
    print(f"{len(changes)} entries would change.")
    return b"".join(output), len(changes)


def atomic_write(target, data, mode, *, gid=None):
    fd, name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            if gid is not None and os.fstat(stream.fileno()).st_gid != gid:
                os.fchown(stream.fileno(), -1, gid)
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, target)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def process_start(pid):
    """Return a kernel start identity, never a rounded ps timestamp."""
    try:
        if sys.platform == "linux":
            # comm can contain spaces and ')'; fields after its final ')' start at 3.
            fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
            return fields[19]  # field 22: start ticks since boot
        if sys.platform == "darwin":
            # proc_bsdinfo from the macOS SDK, PROC_PIDTBSDINFO = 3.
            layout = "=12I16s32s6I2Q"
            size = struct.calcsize(layout)
            buffer = ctypes.create_string_buffer(size)
            probe = ctypes.CDLL("/usr/lib/libproc.dylib").proc_pidinfo
            probe.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                              ctypes.c_void_p, ctypes.c_int]
            probe.restype = ctypes.c_int
            if probe(pid, 3, 0, buffer, size) == size:
                values = struct.unpack(layout, buffer.raw)
                if values[3] == pid and values[-2] > 0:
                    return f"{values[-2]}:{values[-1]}"
    except (OSError, ValueError, IndexError):
        pass
    return None


def system_identity():
    """Identify this machine and boot; unavailable identities disable recovery."""
    try:
        if sys.platform == "linux":
            host = Path("/etc/machine-id").read_text().strip()
            boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        elif sys.platform == "darwin":
            host_output = subprocess.check_output(
                ["/usr/sbin/ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                text=True, timeout=5, stderr=subprocess.DEVNULL,
            )
            host = re.search(r'"IOPlatformUUID"\s*=\s*"([0-9A-Fa-f-]+)"', host_output)[1]
            boot = subprocess.check_output(
                ["/usr/sbin/sysctl", "-n", "kern.bootsessionuuid"],
                text=True, timeout=5, stderr=subprocess.DEVNULL,
            ).strip()
        else:
            return None, None
        uuid_pattern = r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}"
        host_pattern = r"[0-9a-fA-F]{32}" if sys.platform == "linux" else uuid_pattern
        if not re.fullmatch(host_pattern, host) or not re.fullmatch(uuid_pattern, boot):
            return None, None
        return host.lower(), boot.lower()
    except (OSError, subprocess.SubprocessError, TypeError):
        return None, None


def lock_owner():
    host, boot = system_identity()
    try:
        namespace = os.readlink("/proc/self/ns/pid") if sys.platform == "linux" else "host"
    except OSError:
        namespace = None
    return dict(format="histfix-lock-v1", platform=sys.platform, host=host, boot=boot,
                namespace=namespace, pid=os.getpid(), uid=os.getuid(), start=process_start(os.getpid()),
                token=uuid.uuid4().hex)


def abandoned_owner(owner, current):
    """Only a recognized owner on this host can be proved abandoned."""
    if not isinstance(owner, dict) or set(owner) != set(current):
        return False
    if (owner["format"] != "histfix-lock-v1" or owner["platform"] != current["platform"]
            or type(owner["pid"]) is not int or not 0 < owner["pid"] < 2**31
            or type(owner["uid"]) is not int or owner["uid"] != os.getuid()):
        return False
    for key in ("host", "boot", "namespace", "start", "token"):
        if not isinstance(owner[key], str) or not owner[key] or not current[key]:
            return False
    if (not re.fullmatch(r"[0-9a-f]{32}", owner["token"])
            or not re.fullmatch(r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}", owner["boot"])
            or not re.fullmatch(r"[1-9][0-9]*:[0-9]{1,6}" if sys.platform == "darwin" else r"[0-9]+",
                                owner["start"])
            or owner["host"] != current["host"]
            or owner["namespace"] != current["namespace"]):
        return False
    if owner["boot"] != current["boot"]:
        return True  # The same machine has rebooted since acquisition.
    try:
        os.kill(owner["pid"], 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False  # Includes permission-denied process probes.
    start = process_start(owner["pid"])
    return start is not None and start != owner["start"]  # PID reuse


def same_inode(path, info):
    try:
        current = path.lstat()
        return (current.st_dev, current.st_ino) == (info.st_dev, info.st_ino)
    except FileNotFoundError:
        return False


def recover_history_lock(lock, current):
    # Never follow zsh symlinks, block on FIFOs, or read unbounded unknown data.
    info = lock.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077 or info.st_size > 4096):
        return False
    fd = os.open(lock, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if ((opened.st_dev, opened.st_ino, opened.st_mode, opened.st_uid,
             opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns)
                != (info.st_dev, info.st_ino, info.st_mode, info.st_uid,
                    info.st_size, info.st_mtime_ns, info.st_ctime_ns)):
            return False
        try:
            owner = json.loads(stream.read(4097))
        except (ValueError, UnicodeError):
            return False
        if not abandoned_owner(owner, current) or not same_inode(lock, opened):
            return False
        checked = os.fstat(stream.fileno())
        if ((checked.st_mode, checked.st_uid, checked.st_size,
             checked.st_mtime_ns, checked.st_ctime_ns)
                != (opened.st_mode, opened.st_uid, opened.st_size,
                    opened.st_mtime_ns, opened.st_ctime_ns)):
            return False
        lock.unlink()
        return True


@contextmanager
def history_lock(target):
    # A persistent inode serializes inspection, recovery, and the whole write.
    # Never unlink this guard: waiters could otherwise lock different inodes.
    guard = Path(str(target) + ".histfix-lock")
    lock = Path(str(target) + ".LOCK")
    busy = "history is locked by another process; try again (see README for manual recovery)"
    fd = os.open(guard, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    with os.fdopen(fd, "r+b") as guard_stream:
        info = os.fstat(guard_stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or info.st_mode & 0o077):
            raise ValueError("unrecognized histfix recovery guard; see README for manual recovery")
        try:
            fcntl.flock(guard_stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError(busy) from error
        if not same_inode(guard, info):
            raise ValueError("histfix recovery guard changed; try again")
        owner = lock_owner()
        # Publish complete metadata in one exclusive hard-link operation.
        # A crash before publication cannot leave a partial/empty .LOCK.
        staging_fd, staging = tempfile.mkstemp(prefix=".histfix-lock-", dir=target.parent)
        acquired = None
        metadata = os.fdopen(staging_fd, "wb")
        try:
            metadata.write(json.dumps(owner).encode("ascii"))
            metadata.flush()
            os.fsync(metadata.fileno())
            staged_info = os.fstat(metadata.fileno())
            try:
                os.link(staging, lock)
            except FileExistsError as error:
                if not recover_history_lock(lock, owner):
                    raise ValueError(busy) from error
                try:
                    os.link(staging, lock)
                except FileExistsError as race:
                    raise ValueError(busy) from race
            acquired = staged_info
            os.unlink(staging)
            # Also respect zsh's HIST_FCNTL_LOCK; a live writer defeats entry.
            with target.open("r+b") as stream:
                fcntl.lockf(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                yield stream
        finally:
            try:
                if acquired is not None and same_inode(lock, acquired):
                    lock.unlink()
                if os.path.exists(staging):
                    os.unlink(staging)
            finally:
                metadata.close()


def confirm():
    print("Apply these changes? [y/N] ", end="", flush=True)
    return sys.stdin.readline().strip().lower() in ("y", "yes")


def terminate_records(data):
    if data and not data.endswith(b"\n"):
        raw, prefix, command = list(records(data))[-1]
        return data[:-len(raw)] + encode_record(prefix, command)
    return data


def append_records(before, appended):
    return (terminate_records(before) if appended else before) + appended


def prepare_memory_reload(before, after, memory, history_count):
    original = list(records(before))
    updated = list(records(after))
    retained = list(records(memory))
    if len(original) != len(updated):
        raise ValueError("history record count changed; refusing to reload shell history")

    # Preserve memory order: fc -R can import disk events after newer commands.
    # Plain records have no timestamp; zsh assigns one when reading them.
    available = defaultdict(list)
    destinations = defaultdict(set)
    for index, record in enumerate(original):
        key = (record[1].split(b":")[1] if record[1] else None, record[2])
        available[key].append(index)
        destinations[key].add(updated[index][2])
    mapping = []
    for event in retained:
        exact = (event[1].split(b":")[1] if event[1] else None, event[2])
        plain = (None, event[2])
        keys = [key for key in dict.fromkeys((exact, plain)) if available[key]]
        if not keys:
            raise ValueError("current shell has memory-only history; refusing to reload it")
        if len(set().union(*(destinations[key] for key in keys))) != 1:
            raise ValueError("current shell history has ambiguous duplicates; refusing to reload it")
        mapping.append(available[keys[0]].pop())
    if len(retained) != history_count + 1:
        raise ValueError("incomplete shell history snapshot; refusing to reload it")
    if retained[-1][2] != updated[mapping[-1]][2]:
        raise ValueError("cannot reload a changed active history event")
    return b"".join(encode_record(event[1], updated[index][2])
                    for event, index in zip(retained, mapping))


def commit_replacement(target, after, info, backup, saved):
    """Restore the previous undo record if a filesystem write reports failure."""
    recovery = Path(tempfile.mkdtemp(prefix=".histfix-undo-", dir=backup.parent))
    previous = recovery / "previous"

    def cleanup():
        try:
            previous.unlink(missing_ok=True)
            recovery.rmdir()
        except OSError as error:
            print(f"histfix: could not remove undo recovery directory {recovery}: {error}",
                  file=sys.stderr)

    prepared = False
    try:
        if backup.exists():
            # Keep the old inode without allocating another copy of its data.
            os.link(backup, previous)
        prepared = True
        atomic_write(backup, saved, 0o600)
        atomic_write(target, after, stat.S_IMODE(info.st_mode), gid=info.st_gid)
    except OSError as error:
        if prepared:
            try:
                if previous.exists():
                    os.replace(previous, backup)
                else:
                    backup.unlink(missing_ok=True)
            except OSError as recovery_error:
                raise OSError(f"{error}; undo recovery failed; retained files: {recovery}") from recovery_error
        cleanup()
        raise
    # Cleanup failure after commit must not roll back the now-valid undo record.
    cleanup()


def main(argv):
    flush = len(argv) == 2 and argv[0] == "--flush"
    validate = bool(argv and argv[0] == "--validate")
    args = None if flush else arguments(argv[1:] if validate else argv)
    if args is None and not flush:
        return 0
    if validate:
        return DRY_RUN_VALIDATED if args.dry_run else VALIDATED
    filename = os.environ.get("HISTFIX_FILE")
    if not filename:
        raise ValueError("source histfix.plugin.zsh in your shell first")
    target = Path(filename).resolve(strict=not flush)
    if flush:
        try:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
    info = target.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError("history must be a regular file owned by the current user")
    if flush:
        pending = Path(argv[1]).read_bytes()
        if pending:
            list(records(pending))
            with history_lock(target) as stream:
                info = os.fstat(stream.fileno())
                atomic_write(target, append_records(stream.read(), pending),
                             stat.S_IMODE(info.st_mode), gid=info.st_gid)
        return 0
    before = target.read_bytes()
    backup = Path(str(target) + ".histfix-undo.json")
    undo_data = None
    if args.command == "replace":
        after, count = preview(before, args)
        if not count:
            return 0
    else:
        if not backup.exists():
            raise ValueError("no replacement to undo")
        undo_data = backup.read_bytes()
        saved = json.loads(undo_data)
        previous = base64.b64decode(saved["before"], validate=True)
        applied = base64.b64decode(saved["after"], validate=True)
        normalized = terminate_records(applied)
        if not applied or (before != applied and not before.startswith(normalized)):
            raise ValueError("history changed since replacement; refusing to overwrite it")
        appended = b"" if before == applied else before[len(normalized):]
        after = append_records(previous, appended)
        print("Undo the last replacement; keep entries appended since then.")
    if args.dry_run:
        return 0
    if not confirm():
        print("Cancelled. No replacements applied.")
        return 0
    memory_name = os.environ.get("HISTFIX_MEMORY")
    memory_target = Path(memory_name) if memory_name else None
    memory_after = prepare_memory_reload(before, after, memory_target.read_bytes(),
                                         int(os.environ["HISTFIX_HISTORY_COUNT"])) if memory_target else None
    with history_lock(target) as stream:
        current = target.stat()
        if ((current.st_dev, current.st_ino, current.st_mode, current.st_uid, current.st_gid)
                != (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid)
                or stream.read() != before):
            raise ValueError("history changed during preview; run histfix again")
        if memory_target is not None:
            atomic_write(memory_target, memory_after, 0o600)
        if args.command == "replace":
            saved = json.dumps({
                "before": base64.b64encode(before).decode("ascii"),
                "after": base64.b64encode(after).decode("ascii"),
            }).encode("utf-8")
            commit_replacement(target, after, info, backup, saved)
        else:
            if backup.read_bytes() != undo_data:
                raise ValueError("undo record changed during preview; run histfix again")
            atomic_write(target, after, stat.S_IMODE(info.st_mode), gid=info.st_gid)
            try:
                backup.unlink()
            except OSError as error:
                print(f"histfix: could not remove undo backup {backup}: {error}",
                      file=sys.stderr)
    print("History updated." if args.command == "replace" else "Replacement undone.")
    return APPLIED


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except (OSError, ValueError, re.error, KeyError) as error:
        print(f"histfix: {error}", file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        print("\nhistfix: interrupted", file=sys.stderr)
        sys.exit(130)
