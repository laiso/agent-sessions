import re
import socket
from collections.abc import Callable
from pathlib import Path

from .model import Session, SourceError
from .runner import Runner, output

FIELDS = ("session_name", "window_index", "pane_index", "pane_id", "pane_title",
          "pane_current_command", "pane_pid", "pane_dead", "pane_active",
          "window_active", "session_attached", "pane_current_path")
SEPARATOR = "\x1f"
AGENTS = {"claude", "codex", "gemini", "agy", "opencode", "aider", "cursor-agent",
          "copilot", "kimi", "pi"}
SHELLS = {"bash", "zsh", "fish", "sh", "dash", "nu"}


def read_cmdline(proc: Path) -> list[str]:
    raw = (proc / "cmdline").read_bytes()
    return raw.removesuffix(b"\0").decode(errors="replace").split("\0") if raw else []


def read_child(pid: str) -> list[str]:
    """Return argv of the pane's last child process, or of the pane process itself."""
    if not pid.isdecimal():
        return []
    parent = Path("/proc") / pid
    try:
        children = (parent / "task" / pid / "children").read_text().split()
        if children:
            argv = read_cmdline(parent.parent / children[-1])
            if argv:
                return argv
        return read_cmdline(parent)
    except OSError:
        return []


def default_title(title: str, hosts: set[str]) -> bool:
    """True for titles tmux or the shell set automatically (hostname, user@host:cwd)."""
    if title in hosts:
        return True
    return any(re.match(r"[^@\s]+@" + re.escape(host) + r"(?=[:\s]|$)", title) for host in hosts)


def collect(runner: Runner, proc_reader: Callable[[str], list[str]] = read_child) -> list[Session]:
    fmt = SEPARATOR.join("#{" + name + "}" for name in FIELDS)
    raw = output(runner, ["tmux", "list-panes", "-a", "-F", fmt],
                 empty_errors=("no server running", "error connecting"))
    hosts = {socket.gethostname(), socket.gethostname().split(".")[0]}
    rows = []
    for line in raw.split("\n"):
        if not line:
            continue
        try:
            session, win, idx, pid, title, current, shell_pid, dead, active, win_active, attached, cwd = line.split(SEPARATOR)
            key = session, int(win), int(idx)
            focused = int(attached) > 0 and active == win_active == "1"
        except ValueError as exc:
            raise SourceError("invalid tmux list-panes record") from exc
        argv = proc_reader(shell_pid) or ([current] if current else [])
        names = [Path(arg).name for arg in argv]
        agent = next((name for name in names if name in AGENTS), None)
        status = "running"
        if dead == "1":
            status = "dead"
        elif len(names) == 1 and names[0].lstrip("-") in SHELLS:
            status = "shell"
        pane = f"{win}.{idx}"
        if title and not default_title(title, hosts):
            pane += " " + title
        rows.append((key, Session("tmux", session, pane, pid, agent, status,
                                  " ".join(argv) or None, cwd or None, focused)))
    return [row for _, row in sorted(rows, key=lambda item: item[0])]
