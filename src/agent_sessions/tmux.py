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
INTERPRETERS = {"node", "nodejs", "bun", "deno", "npx", "bunx", "python", "python3", "uv", "uvx"}
# Options that consume the next argument, per interpreter family.
OPTION_ARGS = {
    "node": {"-r", "--require", "--import", "--loader", "--experimental-loader", "--env-file",
             "--title", "--input-type", "-C", "--conditions"},
    "python": {"-W", "-X", "--check-hash-based-pycs"},
    "npx": {"-p", "--package", "-c", "--call", "--cache", "--registry"},
    "uv": {"--with", "--python", "-p", "--from", "--directory", "--project"},
    "deno": {"--config", "-c", "--import-map", "--location"},
    "bun": {"--preload", "-r", "--cwd", "--config", "-c"},
}
# Options after which no script follows (inline code).
NO_SCRIPT = {"-e", "--eval", "-p", "--print", "-c"}
SUBCOMMANDS = {"run", "exec", "x", "tool"}


def family(name: str) -> str:
    if name.startswith("python"):
        return "python"
    if name in ("nodejs",):
        return "node"
    if name in ("bunx",):
        return "bun"
    if name in ("uvx",):
        return "uv"
    return name


def script_of(argv: list[str]) -> str | None:
    """Basename of the script or module an interpreter runs, or None."""
    fam = family(Path(argv[0]).name)
    takes_arg = OPTION_ARGS.get(fam, set())
    i = 1
    while i < len(argv):
        arg = argv[i]
        if fam == "python" and arg == "-m" and i + 1 < len(argv):
            return argv[i + 1].split(".")[0]
        if fam == "python" and arg == "-c":
            return None
        if fam in ("node", "bun", "deno") and arg in NO_SCRIPT:
            return None
        if arg == "--":
            return Path(argv[i + 1]).name if i + 1 < len(argv) else None
        if arg.startswith("-"):
            i += 2 if (arg in takes_arg and "=" not in arg) else 1
            continue
        if fam in ("uv", "deno", "bun") and arg in SUBCOMMANDS:
            i += 1
            continue
        return Path(arg).name
    return None


def agent_name(argv: list[str]) -> str | None:
    """Agent from the executable, or from the script an interpreter runs (node .../claude)."""
    if not argv:
        return None
    exe = Path(argv[0]).name
    if exe in AGENTS:
        return exe
    if exe in INTERPRETERS or exe.startswith("python3."):
        script = script_of(argv)
        if script in AGENTS:
            return script
    return None
    if names[0] in AGENTS:
        return names[0]
    if names[0] in INTERPRETERS or names[0].startswith("python3."):
        script = next((name for name in names[1:] if not name.startswith("-")), None)
        if script in AGENTS:
            return script
    return None


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
    # Known shell formats: "user@host", "user@host:<cwd>", "user@host <cwd>" (cwd starts with ~ or /).
    return any(re.fullmatch(r"[^@\s]+@" + re.escape(host) + r"(?::.*|\s+[~/]\S*)?", title) for host in hosts)


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
        agent = agent_name(argv)
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
