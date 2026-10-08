"""Replace text in zsh history records without evaluating shell commands."""

import argparse
import base64
from contextlib import contextmanager
import fcntl
import io
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile


APPLIED = 10
VALIDATED = 11
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


def atomic_write(target, data, mode):
    fd, name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, target)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextmanager
def history_lock(target):
    # Respect both zsh's default .LOCK convention and HIST_FCNTL_LOCK.
    lock = Path(str(target) + ".LOCK")
    try:
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as error:
        raise ValueError("history is locked by another process; try again") from error
    os.close(fd)
    try:
        with target.open("r+b") as stream:
            fcntl.lockf(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
    finally:
        lock.unlink()


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


def main(argv):
    flush = len(argv) == 2 and argv[0] == "--flush"
    validate = bool(argv and argv[0] == "--validate")
    args = None if flush else arguments(argv[1:] if validate else argv)
    if args is None and not flush:
        return 0
    if validate:
        return VALIDATED
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
            with history_lock(target):
                atomic_write(target, append_records(target.read_bytes(), pending),
                             stat.S_IMODE(info.st_mode))
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
    with history_lock(target):
        current = target.stat()
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino) or target.read_bytes() != before:
            raise ValueError("history changed during preview; run histfix again")
        if args.command == "replace":
            saved = json.dumps({
                "before": base64.b64encode(before).decode("ascii"),
                "after": base64.b64encode(after).decode("ascii"),
            }).encode("utf-8")
            atomic_write(backup, saved, 0o600)
        elif backup.read_bytes() != undo_data:
            raise ValueError("undo record changed during preview; run histfix again")
        atomic_write(target, after, stat.S_IMODE(info.st_mode))
        if args.command == "undo":
            backup.unlink()
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
