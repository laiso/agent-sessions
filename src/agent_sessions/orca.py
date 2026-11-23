import json
import os
import platform
import stat
from collections.abc import Iterator, Mapping
from functools import partial
from pathlib import Path

from .model import Session, SourceError
from .runner import Runner, output, run


def cli_candidates(env: Mapping[str, str], *, system: str | None = None, root: Path = Path("/")) -> Iterator[str]:
    explicit = env.get("AGENT_SESSIONS_ORCA") or env.get("ORCA_CLI_COMMAND")
    if explicit:
        yield explicit
        return

    home = Path(env["HOME"]) if env.get("HOME") else Path.home()
    system = platform.system() if system is None else system
    legacy_binaries = ()
    if system == "Darwin":
        bundle = Path("Applications/Orca.app/Contents/Resources/bin/orca")
        legacy = Path("Applications/Orca Desktop.app/Contents/MacOS/orca")
        legacy_binaries = (root / legacy, home / legacy)
        shim = home / ".local/bin/orca"
        binaries = (root / bundle, home / bundle, *legacy_binaries)
    elif system == "Linux":
        config = Path(env.get("XDG_CONFIG_HOME") or home / ".config")
        if not config.is_absolute():
            config = home / ".config"
        shim = config / "orca/linux-orca-cli-shim/orca"
        binaries = (home / ".local/opt/orca/orca.AppImage",)
    else:
        return

    for path in (shim, *binaries):
        if not path.is_absolute():
            continue
        try:
            # Case-insensitive lookup can otherwise resolve the Electron GUI, Orca.
            if path in legacy_binaries and "orca" not in os.listdir(path.parent):
                continue
            info = path.stat()
        except OSError:
            continue
        if (stat.S_ISREG(info.st_mode) and info.st_uid in (0, os.geteuid())
                and not info.st_mode & 0o022 and os.access(path, os.X_OK)):
            yield str(path)
            if path != shim:
                return  # Only a shim can fall back to another installation.


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
    candidates = cli_candidates(os.environ)
    cli = next(candidates, None)
    if not cli:
        raise SourceError("Orca CLI unavailable")

    def read(command: str, action: str, key: str) -> list[dict]:
        nonlocal cli
        try:
            return records(runner, [cli, command, action, "--json"], key)
        except SourceError as exc:
            if not str(exc).startswith("Orca CLI is unavailable"):
                raise
            fallback = next(candidates, None)
            if fallback is None:
                raise
            cli = fallback
        return records(runner, [cli, command, action, "--json"], key)

    bots = read("bot", "list", "bots")
    if not bots:
        return []
    try:
        worktrees = read("worktree", "ps", "worktrees")
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
