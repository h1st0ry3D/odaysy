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

Every command prints one JSON document on stdout, except `path`. Errors go to
stderr and exit non-zero. The config is a small JSON file under
$XDG_CONFIG_HOME/odaysy/config.json (default: ~/.config/odaysy/config.json).

The file is user-owned data, so it is read through one checked descriptor and
published with a same-directory temporary plus rename. A symlink, FIFO, device
or oversized file at the fixed path is refused rather than followed.
"""

import argparse
import fcntl
import json
import os
import re
import secrets
import stat
import sys
import tempfile
from datetime import datetime, timezone

MAX_TITLE = 120
MAX_EVENTS = 100
MAX_BYTES = 131072
ID_PATTERN = re.compile(r"^[a-f0-9]{16}$")
BIDI_CONTROLS = set(range(0x200B, 0x2010)) | set(range(0x202A, 0x202F)) \
    | set(range(0x2060, 0x206A)) | {0xFEFF}


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


def save_config(config):
    directory = config_dir()
    destination = config_path()
    ensure_config_dir()
    payload = (json.dumps(config, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    if len(payload) > MAX_BYTES:
        raise ConfigError("the config would be larger than %d bytes" % MAX_BYTES)

    descriptor = -1
    temporary = ""
    try:
        descriptor, temporary = tempfile.mkstemp(prefix=".config.", suffix=".tmp", dir=directory)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        temporary = ""
        directory_descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except OSError as error:
        raise ConfigError("cannot write the config: %s" % error.strerror) from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary != "":
            try:
                os.unlink(temporary)
            except OSError:
                pass


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
    except ConfigError as error:
        sys.stderr.write("Odaysy: %s\n" % error)
        return 1
    except BrokenPipeError:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
