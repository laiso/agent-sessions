import json
import os
import stat
from collections.abc import Mapping
from functools import partial
from pathlib import Path

from .model import Session, SourceError
from .runner import Runner, output, run


def find_cli(env: Mapping[str, str]) -> str | None:
    explicit = env.get("AGENT_SESSIONS_ORCA") or env.get("ORCA_CLI_COMMAND")
    if explicit:
        return explicit

    home = Path(env["HOME"]) if env.get("HOME") else Path.home()
    config = Path(env.get("XDG_CONFIG_HOME") or home / ".config")
    if not config.is_absolute():
        config = home / ".config"
    installations = (config / "orca/linux-orca-cli-shim/orca", home / ".local/opt/orca/orca.AppImage")
    known_files = {}
    for path in installations:
        if not path.is_absolute():
            continue
        try:
            info = path.stat()
        except OSError:
            continue
        if stat.S_ISREG(info.st_mode) and os.access(path, os.X_OK):
            known_files[info.st_dev, info.st_ino] = path
    if not known_files:
        return None

    # A PATH alias may point to either installation. Invoke the known path
    # itself so a later change to the alias cannot redirect the command.
    for name in ("orca", "orca.AppImage"):
        for directory in os.get_exec_path(env):
            try:
                info = (Path(directory) / name).stat()
            except OSError:
                continue
            match = known_files.get((info.st_dev, info.st_ino))
            if match is not None:
                return str(match)
    return None


def records(runner: Runner, argv: list[str], key: str) -> list[dict]:
    try:
        reply = json.loads(output(runner, argv))
    except ValueError as exc:
        raise SourceError("invalid Orca JSON") from exc
    # worktree ps may return just the result envelope.
    if not isinstance(reply, dict) or reply.get("ok", key == "worktrees") is not True:
        raise SourceError("unsuccessful Orca response")
    result = reply.get("result")
    if not isinstance(result, dict) or not isinstance(result.get(key), list):
        raise SourceError(f"missing Orca {key} array")
    return [row for row in result[key] if isinstance(row, dict)]


def status(bot: dict, home: dict) -> str:
    if bot.get("paused") is True:
        return "paused"
    agents = home.get("agents")
    if not isinstance(agents, list):
        return "unknown"
    if not agents:
        return "stopped"
    states = {a["state"] for a in agents if isinstance(a, dict) and isinstance(a.get("state"), str)}
    if "working" in states:
        return "working"
    if states & {"blocked", "waiting", "permission"}:
        return "blocked"
    if states & {"idle", "done"}:
        return "idle"
    return "unknown"


def optional_text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def collect(runner: Runner | None = None) -> list[Session]:
    if runner is None:
        runner = partial(run, timeout=15)
    cli = find_cli(os.environ)
    if not cli:
        raise SourceError("Orca CLI unavailable")
    bots = records(runner, [cli, "bot", "list", "--json"], "bots")
    if not bots:
        return []
    try:
        worktrees = records(runner, [cli, "worktree", "ps", "--json"], "worktrees")
    except SourceError:
        worktrees = []
    homes = {w["bot"]: w for w in worktrees
             if w.get("botHome") is True and isinstance(w.get("bot"), str)}
    sessions = []
    for bot in bots:
        name = optional_text(bot.get("name"))
        pane_id = optional_text(bot.get("id"))
        if not name or not pane_id:
            continue
        home = homes.get(name, {})
        specialty = optional_text(bot.get("specialty"))
        sessions.append(Session("orca", name, "home", pane_id, optional_text(bot.get("agent")),
                                status(bot, home), specialty, optional_text(home.get("path")),
                                False, specialty=specialty))
    return sorted(sessions, key=lambda row: row.session)
