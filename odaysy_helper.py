#!/usr/bin/python3
"""Storage for Odaysy.

The panel never reads or writes the config file itself. It calls this helper,
which is the only writer and the only reader:

    odaysy_helper.py list
    odaysy_helper.py add -- "Quit smoking"
    odaysy_helper.py rename -- <id> "Quit nicotine"
    odaysy_helper.py reset -- <id>
    odaysy_helper.py pin -- <id>
    odaysy_helper.py unpin
    odaysy_helper.py remove -- <id>
    odaysy_helper.py backup export   # <- {"path": "..."} on stdin: a copy
    odaysy_helper.py backup restore  # <- {"path": "..."} on stdin: that
                                     #    file replaces the list, after the
                                     #    current one is copied into backups/

Every command prints one JSON document on stdout, except `path`. Errors go to
stderr and exit non-zero. The config is a small JSON file under
$XDG_CONFIG_HOME/odaysy/config.json (default: ~/.config/odaysy/config.json).

The file is user-owned data, so it is read through one checked descriptor and
published with a same-directory temporary plus rename. A symlink, FIFO, device
or oversized file at the fixed path is refused rather than followed. Backup
and restore paths come from the panel's own file chooser, so they get a
component-by-component walk and the same checked reads and atomic writes.
"""

import argparse
import errno
import fcntl
import json
import os
import re
import secrets
import stat
import sys
import time
from datetime import datetime, timezone

MAX_TITLE = 120
MAX_EVENTS = 100
MAX_BYTES = 131072
MAX_PATH_LEN = 4096
MAX_PATH_DEPTH = 40
MAX_LINK_HOPS = 8
SNAPSHOT_KEEP = 10
BACKUP_DIR = "backups"
ID_PATTERN = re.compile(r"^[a-f0-9]{16}$")
SNAPSHOT_PATTERN = re.compile(r"^pre-restore_\d{8}_\d{6}-[0-9a-f]{8}\.json$")
BIDI_CONTROLS = set(range(0x200B, 0x2010)) | set(range(0x202A, 0x202F)) \
    | set(range(0x2060, 0x206A)) | {0xFEFF}
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class ConfigError(Exception):
    pass


def config_dir():
    base = os.environ.get("XDG_CONFIG_HOME", "").strip()
    if base == "":
        home = os.environ.get("HOME", "").strip()
        if home == "":
            raise ConfigError("HOME is not set, so the config path is unknown")
        base = os.path.join(home, ".config")
    return os.path.join(base, "odaysy")


def config_path():
    return os.path.join(config_dir(), "config.json")


def backup_dir_path():
    return os.path.join(config_dir(), BACKUP_DIR)


def lock_path():
    return os.path.join(config_dir(), ".lock")


def empty_config():
    return {"version": 1, "pinnedId": "", "events": []}


def sanitize_title(value):
    if not isinstance(value, str):
        raise ConfigError("the event name must be text")
    kept = []
    for character in value:
        code = ord(character)
        if code in (9, 10, 13):
            kept.append(" ")
            continue
        if code < 0x20 or 0x7F <= code <= 0x9F or code in BIDI_CONTROLS:
            continue
        kept.append(character)
    title = " ".join("".join(kept).split())
    if title == "":
        raise ConfigError("the event name is empty")
    if len(title) > MAX_TITLE:
        raise ConfigError("the event name is longer than %d characters" % MAX_TITLE)
    return title


def parse_since(value):
    if not isinstance(value, str) or len(value) > 40:
        raise ConfigError("an event timestamp is not a valid date")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ConfigError("an event timestamp is not a valid date") from None
    if parsed.tzinfo is None:
        raise ConfigError("an event timestamp has no timezone")
    parsed = parsed.astimezone(timezone.utc)
    if not 1970 <= parsed.year <= 2100:
        raise ConfigError("an event timestamp is outside 1970-2100")
    return parsed


def format_since(value):
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def now_since():
    return datetime.now(timezone.utc)


def normalize_config(raw):
    if not isinstance(raw, dict):
        raise ConfigError("the config is not a JSON object")
    if raw.get("version") != 1:
        raise ConfigError("the config version is not 1")
    entries = raw.get("events")
    if not isinstance(entries, list):
        raise ConfigError("the config has no events list")
    if len(entries) > MAX_EVENTS:
        raise ConfigError("the config has more than %d events" % MAX_EVENTS)

    events = []
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ConfigError("an event is not a JSON object")
        identifier = entry.get("id")
        if not isinstance(identifier, str) or ID_PATTERN.fullmatch(identifier) is None:
            raise ConfigError("an event has an invalid id")
        if identifier in seen:
            raise ConfigError("two events have the same id")
        seen.add(identifier)
        events.append({
            "id": identifier,
            "title": sanitize_title(entry.get("title")),
            "since": format_since(parse_since(entry.get("since"))),
        })

    pinned = raw.get("pinnedId", "")
    if not isinstance(pinned, str) or pinned not in seen:
        pinned = ""
    return {"version": 1, "pinnedId": pinned, "events": events}


def read_bytes(path):
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ConfigError("cannot open the config: %s" % error.strerror) from None

    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ConfigError("the config is not a regular file")
        if info.st_uid != os.getuid():
            raise ConfigError("the config is not owned by this user")
        if info.st_size > MAX_BYTES:
            raise ConfigError("the config is larger than %d bytes" % MAX_BYTES)
        chunks = []
        total = 0
        while True:
            chunk = os.read(descriptor, 65536)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_BYTES:
                raise ConfigError("the config is larger than %d bytes" % MAX_BYTES)
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def load_config():
    payload = read_bytes(config_path())
    if payload is None:
        return empty_config()
    try:
        raw = json.loads(payload.decode("utf-8"))
    except UnicodeDecodeError:
        raise ConfigError("the config is not UTF-8") from None
    except json.JSONDecodeError:
        raise ConfigError("the config is not valid JSON") from None
    return normalize_config(raw)


def ensure_config_dir():
    directory = config_dir()
    try:
        os.makedirs(directory, mode=0o700, exist_ok=True)
    except OSError as error:
        raise ConfigError("cannot create the config directory: %s" % error.strerror) from None
    try:
        info = os.lstat(directory)
    except OSError as error:
        raise ConfigError("cannot inspect the config directory: %s" % error.strerror) from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ConfigError("the config directory is not a directory")
    if info.st_uid != os.getuid():
        raise ConfigError("the config directory is not owned by this user")
    try:
        os.chmod(directory, 0o700)
    except OSError as error:
        raise ConfigError("cannot make the config directory private: %s" % error.strerror) from None


def ensure_backup_dir():
    directory = backup_dir_path()
    ensure_config_dir()
    try:
        os.makedirs(directory, mode=0o700, exist_ok=True)
    except OSError as error:
        raise ConfigError("cannot create the backup directory: %s" % error.strerror) from None
    try:
        info = os.lstat(directory)
    except OSError as error:
        raise ConfigError("cannot inspect the backup directory: %s" % error.strerror) from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ConfigError("the backup directory is not a directory")
    if info.st_uid != os.getuid():
        raise ConfigError("the backup directory is not owned by this user")
    try:
        os.chmod(directory, 0o700)
    except OSError as error:
        raise ConfigError("cannot make the backup directory private: %s" % error.strerror) from None
    try:
        return os.open(directory, _DIR_FLAGS)
    except OSError as error:
        raise ConfigError("cannot open the backup directory: %s" % error.strerror) from None


def write_atomic(dir_fd, name, data):
    if len(data) > MAX_BYTES:
        raise ConfigError("the file would be larger than %d bytes" % MAX_BYTES)
    temporary = ".%s.%s.tmp" % (name, secrets.token_hex(8))
    descriptor = -1
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600, dir_fd=dir_fd)
        os.fchmod(descriptor, 0o600)
        view = memoryview(data)
        while view:
            view = view[os.write(descriptor, view):]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.rename(temporary, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        os.fsync(dir_fd)
    except OSError as error:
        raise ConfigError("cannot write the file: %s" % error.strerror) from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=dir_fd)
        except OSError:
            pass


def read_bounded(dir_fd, name, limit=MAX_BYTES):
    try:
        descriptor = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            dir_fd=dir_fd)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ConfigError("cannot open that file: %s" % error.strerror) from None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ConfigError("that file is not a regular file")
        if info.st_uid != os.getuid():
            raise ConfigError("that file is not owned by this user")
        if info.st_size > limit:
            raise ConfigError("that file is larger than %d bytes" % limit)
        chunks = []
        total = 0
        while True:
            chunk = os.read(descriptor, 65536)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise ConfigError("that file is larger than %d bytes" % limit)
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def snapshot_config(raw):
    directory = ensure_backup_dir()
    try:
        name = "pre-restore_%s-%s.json" % (
            time.strftime("%Y%m%d_%H%M%S", time.localtime()), secrets.token_hex(4))
        write_atomic(directory, name, raw)
        prune_snapshots(directory)
    finally:
        os.close(directory)


def prune_snapshots(dir_fd):
    entries = []
    for name in os.listdir(dir_fd):
        if SNAPSHOT_PATTERN.fullmatch(name) is None:
            continue
        try:
            info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except OSError:
            continue
        if stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid():
            entries.append((info.st_mtime, name))
    if len(entries) <= SNAPSHOT_KEEP:
        return
    entries.sort()
    for _, name in entries[:len(entries) - SNAPSHOT_KEEP]:
        try:
            os.unlink(name, dir_fd=dir_fd)
        except OSError:
            pass


def save_config(config):
    directory = config_dir()
    ensure_config_dir()
    payload = (json.dumps(config, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    try:
        dir_fd = os.open(directory, _DIR_FLAGS)
    except OSError as error:
        raise ConfigError("cannot open the config directory: %s" % error.strerror) from None
    try:
        write_atomic(dir_fd, "config.json", payload)
    finally:
        os.close(dir_fd)


class config_lock:
    """Serialize read-modify-write so two clicks cannot lose one another."""

    def __enter__(self):
        ensure_config_dir()
        try:
            self.descriptor = os.open(
                lock_path(), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        except OSError as error:
            raise ConfigError("cannot open the config lock: %s" % error.strerror) from None
        try:
            info = os.fstat(self.descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise ConfigError("the config lock is not a regular file owned by this user")
            os.fchmod(self.descriptor, 0o600)
        except Exception:
            os.close(self.descriptor)
            raise
        try:
            fcntl.flock(self.descriptor, fcntl.LOCK_EX)
        except Exception:
            os.close(self.descriptor)
            raise
        return self

    def __exit__(self, _kind, _value, _traceback):
        try:
            fcntl.flock(self.descriptor, fcntl.LOCK_UN)
        finally:
            os.close(self.descriptor)
        return False


def _ok_component(name):
    return (name not in ("", ".", "..") and "/" not in name and "\0" not in name
            and not any(ord(character) < 0x20 for character in name))


def _link_components(base, target):
    out = list(base)
    for part in target.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if not out:
                raise ConfigError("a link in the chosen path climbs above the root")
            out.pop()
            continue
        out.append(part)
    return out


def _check_chosen_dir(dir_fd, name):
    info = os.fstat(dir_fd)
    if not stat.S_ISDIR(info.st_mode):
        raise ConfigError("not a directory: %s" % name)
    if info.st_uid not in (0, os.geteuid()):
        raise ConfigError("not yours: %s" % name)
    if info.st_mode & 0o022 and not info.st_mode & 0o1000:
        raise ConfigError("%s is writable by other accounts" % name)


def open_chosen_dir(parts):
    if not parts or len(parts) > MAX_PATH_DEPTH or not all(_ok_component(p) for p in parts):
        raise ConfigError("refusing that path")
    root_fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    descriptor = os.dup(root_fd)
    try:
        pending = list(parts)
        prefix = []
        hops = 0
        while pending:
            name = pending.pop(0)
            try:
                next_fd = os.open(name, _DIR_FLAGS, dir_fd=descriptor)
            except OSError as error:
                if error.errno not in (errno.ENOTDIR, errno.ELOOP):
                    raise ConfigError("cannot open that folder: %s" % error.strerror) from None
                try:
                    info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                except OSError:
                    raise ConfigError("cannot open that folder: %s" % error.strerror) from None
                if not stat.S_ISLNK(info.st_mode):
                    raise ConfigError("not a directory: %s" % name) from None
                hops += 1
                if hops > MAX_LINK_HOPS:
                    raise ConfigError("too many links in the chosen path")
                try:
                    target = os.readlink(name, dir_fd=descriptor)
                except OSError as link_error:
                    raise ConfigError("cannot read a link in the chosen path: %s"
                                      % link_error.strerror) from None
                if not target or len(target) > MAX_PATH_LEN or "\0" in target:
                    raise ConfigError("refusing a link in the chosen path")
                base = [] if target.startswith("/") else prefix
                pending = _link_components(base, target) + pending
                if len(pending) > MAX_PATH_DEPTH:
                    raise ConfigError("chosen path too deep")
                os.close(descriptor)
                descriptor = os.dup(root_fd)
                prefix = []
                continue
            os.close(descriptor)
            descriptor = next_fd
            _check_chosen_dir(descriptor, name)
            prefix.append(name)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise
    finally:
        os.close(root_fd)


def chosen_path(path):
    parts = [part for part in path.split("/") if part]
    if not parts or any(part in (".", "..") for part in parts):
        raise ConfigError("refusing that path")
    name = parts.pop()
    if not _ok_component(name):
        raise ConfigError("refusing that file name")
    return open_chosen_dir(parts), name


def request_path():
    payload = sys.stdin.buffer.readline(MAX_BYTES + 1)
    if len(payload) > MAX_BYTES:
        raise ConfigError("the request is too large")
    if not payload.strip():
        raise ConfigError("the request is empty")
    try:
        request = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ConfigError("the request is not valid JSON") from None
    if not isinstance(request, dict):
        raise ConfigError("the request is not a JSON object")
    path = request.get("path")
    if not isinstance(path, str) or not path.startswith("/") or len(path) > MAX_PATH_LEN:
        raise ConfigError("the request needs an absolute file path")
    if "\0" in path or any(ord(character) < 0x20 for character in path):
        raise ConfigError("the path contains a control character")
    return path


def emit_document(document, verb, message):
    document = dict(document)
    document["ok"] = True
    document["verb"] = verb
    document["message"] = message
    emit(document)


def command_backup_export(_args):
    path = request_path()
    dir_fd, name = chosen_path(path)
    try:
        raw = read_bytes(config_path())
        if raw is None:
            raise ConfigError("there is nothing to back up yet")
        write_atomic(dir_fd, name, raw)
    finally:
        os.close(dir_fd)
    emit_document(state_document(load_config()), "export", "Wrote %s." % name)


def command_backup_restore(_args):
    path = request_path()
    dir_fd, name = chosen_path(path)
    try:
        raw = read_bounded(dir_fd, name)
    finally:
        os.close(dir_fd)
    if raw is None:
        raise ConfigError("that file is not there")
    try:
        incoming = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ConfigError("that file is not a valid backup") from None
    try:
        document = normalize_config(incoming)
    except ConfigError as error:
        raise ConfigError("that file is not a backup Odaysy wrote: %s" % error) from None
    if not document["events"]:
        raise ConfigError("that backup holds no events")
    with config_lock():
        previous = read_bytes(config_path())
        if previous is not None:
            snapshot_config(previous)
        save_config(document)
    emit_document(
        state_document(document), "restore",
        "Restored %d events. The previous list is in backups/." % len(document["events"]))


def new_id(config):
    known = {entry["id"] for entry in config["events"]}
    while True:
        identifier = secrets.token_hex(8)
        if identifier not in known:
            return identifier


def find_event(config, identifier):
    for entry in config["events"]:
        if entry["id"] == identifier:
            return entry
    raise ConfigError("no event has that id")


def mutate(change):
    with config_lock():
        config = load_config()
        change(config)
        save_config(config)
        return state_document(config)


def state_document(config):
    return {
        "version": 1,
        "configPath": config_path(),
        "pinnedId": config["pinnedId"],
        "events": config["events"],
    }


def emit(document):
    sys.stdout.write(json.dumps(document, ensure_ascii=False, separators=(",", ":")) + "\n")


def command_list(_args):
    emit(state_document(load_config()))


def command_path(_args):
    sys.stdout.write(config_path() + "\n")


def command_add(args):
    title = sanitize_title(args.title)

    def change(config):
        if len(config["events"]) >= MAX_EVENTS:
            raise ConfigError("the list is full (%d events)" % MAX_EVENTS)
        first_event = len(config["events"]) == 0
        identifier = new_id(config)
        config["events"].append({
            "id": identifier,
            "title": title,
            "since": format_since(now_since()),
        })
        # The first event is the only thing in the list, so there is nothing
        # ambiguous about which counter belongs in the bar. Pin it now: the
        # radio is selected from the moment the row appears, and adding the
        # second event cannot silently change what the bar is showing.
        if first_event:
            config["pinnedId"] = identifier

    emit(mutate(change))


def command_rename(args):
    title = sanitize_title(args.title)

    def change(config):
        find_event(config, args.id)["title"] = title

    emit(mutate(change))


def command_reset(args):
    def change(config):
        find_event(config, args.id)["since"] = format_since(now_since())

    emit(mutate(change))


def command_pin(args):
    def change(config):
        find_event(config, args.id)
        config["pinnedId"] = args.id

    emit(mutate(change))


def command_unpin(_args):
    def change(config):
        config["pinnedId"] = ""

    emit(mutate(change))


def command_remove(args):
    def change(config):
        find_event(config, args.id)
        config["events"] = [entry for entry in config["events"] if entry["id"] != args.id]
        if config["pinnedId"] == args.id:
            config["pinnedId"] = ""

    emit(mutate(change))


def build_parser():
    parser = argparse.ArgumentParser(prog="odaysy_helper.py")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("list").set_defaults(run=command_list)
    subparsers.add_parser("path").set_defaults(run=command_path)
    subparsers.add_parser("unpin").set_defaults(run=command_unpin)

    backup = subparsers.add_parser("backup")
    backup_commands = backup.add_subparsers(dest="backup_command", required=True)
    backup_commands.add_parser("export").set_defaults(run=command_backup_export)
    backup_commands.add_parser("restore").set_defaults(run=command_backup_restore)

    add = subparsers.add_parser("add")
    add.add_argument("title")
    add.set_defaults(run=command_add)

    rename = subparsers.add_parser("rename")
    rename.add_argument("id")
    rename.add_argument("title")
    rename.set_defaults(run=command_rename)

    for name, run in (("reset", command_reset), ("pin", command_pin), ("remove", command_remove)):
        command = subparsers.add_parser(name)
        command.add_argument("id")
        command.set_defaults(run=run)

    return parser


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    try:
        args = build_parser().parse_args(argv)
        args.run(args)
    except BrokenPipeError:
        return 0
    except (ConfigError, OSError, ValueError) as error:
        if isinstance(error, OSError):
            message = error.strerror or str(error)
        else:
            message = str(error)
        sys.stderr.write("Odaysy: %s\n" % message)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
