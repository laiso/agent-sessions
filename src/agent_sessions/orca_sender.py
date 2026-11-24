"""Manage the private sender terminal and serialize its use across CLI processes."""
import fcntl
import json
import os
import re
import shutil
import stat
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from . import orca
from .model import SourceError
from .render import cell
from .target import TargetError


def resolve(client, name):
    bots = orca.bot_sessions(client("bots", "bot", "list", "--json"), [])
    matches = [bot for bot in bots if bot.session == name]
    if len(matches) == 1:
        return matches[0]
    if matches:
        raise TargetError(5, f"ambiguous bot: orca:{cell(name)}", [])
    reason = f"target not found: orca:{cell(name)}"
    if bots:
        reason += "\ndid you mean:\n" + "\n".join(f"  orca:{cell(b.session)}" for b in bots)
    raise TargetError(4, reason, [])


@contextmanager
def locked_state():
    base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    if not base.is_absolute():
        base = Path.home() / ".local/state"
    folder = base / "agent-sessions"
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = folder.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise SourceError("unsafe Orca sender state directory")
    lock = os.open(folder / "orca-sender.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(lock)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise SourceError("unsafe Orca sender lock")
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield folder / "orca-sender.json"
    finally:
        os.close(lock)


def valid_handle(value):
    return isinstance(value, str) and re.fullmatch(r"term_[A-Za-z0-9_-]+", value)


def load(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, encoding="utf-8") as stream:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
                return None
            data = json.load(stream)
        if (isinstance(data, dict) and valid_handle(data.get("handle"))
                and isinstance(data.get("cli"), str) and data["cli"]
                and isinstance(data.get("created_at"), str)):
            datetime.fromisoformat(data["created_at"])
            return data
    except (OSError, ValueError):
        pass
    return None


def save(path, handle, cli):
    with tempfile.NamedTemporaryFile(dir=path.parent, mode="w", delete=False) as stream:
        tmp = Path(stream.name)
        try:
            json.dump(dict(handle=handle, cli=cli, created_at=datetime.now(timezone.utc).isoformat()), stream)
            stream.flush()
            os.fsync(stream.fileno())
            tmp.replace(path)
        finally:
            tmp.unlink(missing_ok=True)


def terminals(client):
    rows = client("terminals", "terminal", "list", "--json")
    found = {}
    for row in rows:
        handle = row.get("handle")
        if not valid_handle(handle) or handle in found or not isinstance(row.get("orphaned"), bool):
            raise SourceError("invalid Orca terminal inventory")
        found[handle] = row
    return found


def manage(action, runner=None, *, client=None, name=None, kind=None, subject=None, message=None):
    with locked_state() as path:
        state = load(path)
        if not state and action != "send":
            if action == "close":
                path.unlink(missing_ok=True)
            return "No saved sender terminal."
        client = client or orca.reader(runner)
        inventory = terminals(client)
        cli = os.path.abspath(shutil.which(client.cli) or client.cli)
        if state and state["cli"] != cli:
            raise SourceError("sender state belongs to another Orca CLI")
        handle = state["handle"] if state else None
        row = inventory.get(handle)
        if action == "status":
            status = "missing" if row is None else "orphaned" if row["orphaned"] else "available"
            return f"Sender terminal {handle}: {status}"
        if action == "close":
            if row is not None:
                tab = row.get("tabId")
                if not tab or any(other.get("tabId") == tab for h, other in inventory.items() if h != handle):
                    raise SourceError("cannot confirm the saved terminal has its own tab")
                client.mutate("terminal", "close", "--terminal", handle, "--tab")
            path.unlink()
            return "Sender terminal closed." if row else "Saved terminal was already gone."
        if row is None or row["orphaned"]:
            reply = client.mutate("terminal", "create", "--title", "agent-sessions")
            result = reply.get("result")
            terminal = result.get("terminal") if isinstance(result, dict) else None
            handle = terminal.get("handle") if isinstance(terminal, dict) else None
            if not valid_handle(handle) or handle in inventory:
                raise SourceError("create did not identify a new sender terminal")
            try:
                save(path, handle, cli)
            except OSError as exc:
                raise SourceError(f"could not save newly created terminal {handle}: {exc}") from exc
            # Keep all subsequent operations bound to the instance used to create it.
            fresh = orca.records(client.runner, [client.cli, "terminal", "list", "--json"], "terminals")
            matches = [r for r in fresh if r.get("handle") == handle]
            if len(matches) != 1 or matches[0].get("orphaned") is not False:
                raise SourceError("created sender terminal missing or orphaned")
        try:
            client.mutate("orchestration", "send", "--to", "bot:" + name, "--from", handle,
                          "--type", kind, f"--subject={subject}", f"--body={message}")
        except SourceError as exc:
            raise SourceError(f"delivery not confirmed: {exc}") from exc
