import json
import os
import shutil
from collections.abc import Callable, Mapping
from functools import partial

from .model import Session, SourceError
from .runner import Runner, output, run


def screen_reader(path: str) -> bool:
    """True for GNOME's Orca screen reader, which is also installed as `orca`.

    Running it with `bot list --json` could start speech output, so a PATH hit
    that is that Python program is skipped. Unreadable files are not rejected.
    """
    try:
        with open(path, "rb") as handle:
            head = handle.read(65536)
    except OSError:
        return False
    return head.startswith(b"#!") and b"python" in head.split(b"\n", 1)[0] and (
        b"from orca import" in head or b"import orca" in head or b"screen reader" in head.lower())


def find_cli(env: Mapping[str, str], which: Callable[[str], str | None],
             is_screen_reader: Callable[[str], bool] = screen_reader) -> str | None:
    explicit = env.get("AGENT_SESSIONS_ORCA") or env.get("ORCA_CLI_COMMAND")
    if explicit:
        return explicit
    for name in ("orca", "orca.AppImage"):
        path = which(name)
        if path and not is_screen_reader(path):
            return path
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
    cli = find_cli(os.environ, shutil.which)
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
