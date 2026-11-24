import platform
import re
import socket
from collections.abc import Callable
from pathlib import Path

from .model import Session, SourceError, TmuxSession
from .runner import Runner, output

FIELDS = ("session_name", "window_index", "pane_index", "pane_id", "pane_title",
          "pane_current_command", "pane_pid", "pane_dead", "pane_active",
          "window_active", "session_attached", "pane_current_path")
DELIVERY_IDENTITY = "#{pid}:#{start_time}:#{pane_pid}"
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
    "npx": {"-p", "--package", "-c", "--call", "--cache", "--registry", "-w", "--workspace",
            "--userconfig", "--prefix"},
    "uv": {"--with", "--with-requirements", "--with-editable", "--python", "-p", "--from",
           "--directory", "--project", "--env-file", "--index", "--extra", "--group"},
    "deno": {"--config", "-c", "--import-map", "--location", "--env-file"},
    "bun": {"--preload", "-r", "--cwd", "--config", "-c", "--env-file"},
}
# Options whose value is inline code: no script follows.
INLINE_CODE = {"node": {"-e", "--eval", "-p", "--print"}, "python": {"-c"},
               "bun": {"-e", "--eval", "-p", "--print"}, "deno": set()}
# Launch subcommands consumed once, before the command name (uv run X, deno run X, bun run X).
SUBCOMMANDS = {"uv": {"run", "tool"}, "deno": {"run"}, "bun": {"run", "x"}}


def family(name: str) -> str:
    if name.startswith("python"):
        return "python"
    return {"nodejs": "node", "bunx": "bun", "uvx": "uv"}.get(name, name)


def script_of(argv: list[str]) -> str | None:
    """Basename of the script or module an interpreter runs, or None.

    Best effort: covers common option forms (separate value, --opt=value, and
    Python's joined short options -mmod / -ccode); unknown options are assumed
    to take no value.
    """
    fam = family(Path(argv[0]).name)
    takes_arg = OPTION_ARGS.get(fam, set())
    inline = INLINE_CODE.get(fam, set())
    subcommands = set(SUBCOMMANDS.get(fam, set()))
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg == "--":
            return Path(argv[i + 1]).name if i + 1 < len(argv) else None
        if arg.startswith("-"):
            name, has_value = arg.split("=", 1)[0], "=" in arg
            if fam == "python" and not arg.startswith("--"):
                flag, joined = arg[:2], arg[2:]
                if flag == "-m":
                    module = joined or (argv[i + 1] if i + 1 < len(argv) else "")
                    return module.split(".")[0] or None
                if flag == "-c":
                    return None
                i += 2 if (flag in takes_arg and not joined) else 1
                continue
            if name in inline:
                return None
            i += 2 if (name in takes_arg and not has_value) else 1
            continue
        if arg in subcommands:
            subcommands = set()  # only one launch subcommand
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
    return any(re.fullmatch(r"[^@\s]+@" + re.escape(host) + r"(?::.*|\s+[~/].*)?", title) for host in hosts)


def mac_commands(runner: Runner) -> dict[str, str]:
    """Index one ps snapshot by pane PID, preferring the newest direct child."""
    try:
        raw = output(runner, ["ps", "-A", "-ww", "-o", "pid=,ppid=,args="])
    except SourceError:
        return {}
    commands = {}
    newest = {}
    for line in raw.splitlines():
        fields = line.split(None, 2)
        if len(fields) < 2 or not all(value.isascii() and value.isdigit() for value in fields[:2]):
            return {}
        pid, parent = map(int, fields[:2])
        if len(fields) == 3:
            commands[pid] = fields[2]
        if pid > newest.get(parent, -1):
            newest[parent] = pid
    return {str(pid): commands.get(newest.get(pid), commands.get(pid, ""))
            for pid in commands.keys() | newest.keys()}


def validated_identity(target: Session) -> str:
    if not isinstance(target, TmuxSession) or not re.fullmatch(r"%[0-9]+", target.pane_id):
        raise SourceError("missing or invalid tmux pane identity")
    # tmux can clear pane_pid after a retained pane exits.
    pattern = r"[0-9]+:[0-9]+:[0-9]*" if target.status == "dead" else r"[0-9]+:[0-9]+:[0-9]+"
    if not re.fullmatch(pattern, target.delivery_identity):
        raise SourceError("missing or invalid tmux pane identity")
    return target.delivery_identity


def collect(runner: Runner, proc_reader: Callable[[str], list[str]] | None = None,
            *, system: str | None = None) -> list[Session]:
    fmt = SEPARATOR.join("#{" + name + "}" for name in FIELDS)
    fmt += SEPARATOR + DELIVERY_IDENTITY
    raw = output(runner, ["tmux", "list-panes", "-a", "-F", fmt],
                 empty_errors=r"no server running(?: on .+)?|error connecting to .+ \((?:No such file or directory|Connection refused)\)")
    if not raw:
        return []
    system = platform.system() if system is None else system
    commands = mac_commands(runner) if system == "Darwin" and proc_reader is None else {}
    if proc_reader is None and system == "Linux":
        proc_reader = read_child
    hosts = {socket.gethostname(), socket.gethostname().split(".")[0]}
    rows = []
    for line in raw.split("\n"):
        if not line:
            continue
        try:
            session, win, idx, pid, title, current, shell_pid, dead, active, win_active, attached, cwd, identity = line.split(SEPARATOR)
            key = session, int(win), int(idx)
            is_active = active == win_active == "1"
            focused = int(attached) > 0 and is_active
        except ValueError as exc:
            raise SourceError("invalid tmux list-panes record") from exc
        command = commands.get(shell_pid, "")
        argv = proc_reader(shell_pid) if proc_reader else command.split()
        argv = argv or ([current] if current else [])
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
        rows.append((key, TmuxSession("tmux", session, pane, pid, agent, status,
                                  command or " ".join(argv) or None, cwd or None, focused, is_active, identity)))
    return [row for _, row in sorted(rows, key=lambda item: item[0])]
