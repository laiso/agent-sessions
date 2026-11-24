"""Black-box test helpers for the independent spec-based agent-sessions test suite."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
CLI_PATH = REPO_ROOT / "agent-sessions"

READ_SCHEMA_KEYS = frozenset(
    {
        "source",
        "target",
        "id",
        "type",
        "structured",
        "from",
        "to",
        "subject",
        "body",
        "timestamp",
        "thread_id",
    }
)

LIST_SCHEMA_KEYS = frozenset(
    {
        "source",
        "session",
        "pane",
        "pane_id",
        "agent",
        "status",
        "command",
        "cwd",
        "focused",
        "specialty",
    }
)

ISO_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)$")


def clean_base_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Return a clean environment stripped of ambient tmux/Orca override vars."""
    env = os.environ.copy()
    for key in (
        "TMUX",
        "TMUX_PANE",
        "TMUX_TMPDIR",
        "AGENT_SESSIONS_ORCA",
        "ORCA_CLI_COMMAND",
    ):
        env.pop(key, None)
    if extra:
        env.update(extra)
    return env


def run_cli(
    args: list[str],
    env: dict[str, str] | None = None,
    timeout: float = 30.0,
    *, instrumentation: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Execute ./agent-sessions as a black-box subprocess."""
    command = [str(CLI_PATH)]
    if instrumentation is not None:
        # Instrument the module directly. The production launcher isolates its import path.
        env = dict(env or os.environ, PYTHONPATH=os.pathsep.join(map(str, (REPO_ROOT / "src", instrumentation))))
        command = [sys.executable, "-P", "-m", "agent_sessions"]
    return subprocess.run(
        [*command, *args],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def write_executable(path: Path, body: str, mode: int = 0o755) -> Path:
    """Write a Python or shell script and chmod it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(mode)
    return path


def composer_fixture(config):
    """Source for the standalone fake CLIs' per-pane draft and transcript."""
    state_path = config.with_suffix(".screen")
    state_path.unlink(missing_ok=True)
    return f"""
from pathlib import Path
state_path = Path({str(state_path)!r})
screens = json.loads(state_path.read_text()) if state_path.exists() else {{}}
def screen(pid):
    return screens.setdefault(pid, {{"draft": "", "history": []}})
def input_text(pid, data):
    view = screen(pid)
    if data == "\\n":
        view["history"].append("› " + view["draft"])
        view["draft"] = ""
    else:
        view["draft"] += data
    state_path.write_text(json.dumps(screens))
def visible(pid):
    view = screen(pid)
    return "\\n".join([*view["history"], "› " + view["draft"]])
"""


class FakeEnvironment:
    """Isolated temp filesystem + fake PATH/HOME/XDG_CONFIG_HOME for black-box tests."""

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="agent-sessions-spec-")
        self.root = Path(self._tmp.name)
        self.bin_dir = self.root / "bin"
        self.home_dir = self.root / "home"
        self.xdg_dir = self.root / "xdg"
        self.state_dir = self.root / "state"
        for d in (self.bin_dir, self.home_dir, self.xdg_dir, self.state_dir):
            d.mkdir(parents=True, exist_ok=True)
        # Provide real python3 (sys.executable, not a mise shim) and basic POSIX tools
        # needed by the launcher, while excluding herdr/tmux/orca from PATH.
        py_dst = self.bin_dir / "python3"
        if not py_dst.exists():
            os.symlink(sys.executable, py_dst)
        for util in ("realpath", "dirname", "sh", "env", "basename"):
            util_src = shutil.which(util)
            util_dst = self.bin_dir / util
            if util_src and not util_dst.exists():
                os.symlink(util_src, util_dst)

    def cleanup(self) -> None:
        self._tmp.cleanup()

    def __enter__(self) -> "FakeEnvironment":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.cleanup()

    def env(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        base = clean_base_env(
            {
                "PATH": str(self.bin_dir),
                "HOME": str(self.home_dir),
                "XDG_CONFIG_HOME": str(self.xdg_dir),
            }
        )
        if extra:
            base.update(extra)
        return base

    def install_script(self, name: str, python_body: str, mode: int = 0o755) -> Path:
        script = f"#!{sys.executable}\n{python_body}"
        return write_executable(self.bin_dir / name, script, mode=mode)

    def install_orca_shim(self, python_body: str, mode: int = 0o755) -> Path:
        shim = self.xdg_dir / "orca" / "linux-orca-cli-shim" / "orca"
        script = f"#!{sys.executable}\n{python_body}"
        return write_executable(shim, script, mode=mode)

    def install_orca_appimage(self, python_body: str, mode: int = 0o755) -> Path:
        app = self.home_dir / ".local" / "opt" / "orca" / "orca.AppImage"
        script = f"#!{sys.executable}\n{python_body}"
        return write_executable(app, script, mode=mode)

    def install_herdr_stub(
        self,
        *,
        snapshot: dict[str, Any] | None = None,
        process_info: dict[str, dict[str, Any]] | None = None,
        pane_read: dict[str, tuple[int, str, str]] | None = None,
        pane_send_rc: int = 0,
        pane_send_stderr: str = "",
        snapshot_rc: int = 0,
        snapshot_stderr: str = "",
    ) -> Path:
        """Install a fake `herdr` CLI speaking Herdr's JSON/text protocol."""
        cfg_path = self.state_dir / "herdr_cfg.json"
        log_path = self.state_dir / "herdr_calls.jsonl"
        cfg = {
            "snapshot": snapshot,
            "process_info": process_info or {},
            "pane_read": pane_read or {},
            "pane_send_rc": pane_send_rc,
            "pane_send_stderr": pane_send_stderr,
            "snapshot_rc": snapshot_rc,
            "snapshot_stderr": snapshot_stderr,
        }
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
        body = f"""
import json, sys
cfg = json.loads(open({str(cfg_path)!r}, encoding="utf-8").read())
{composer_fixture(cfg_path)}
argv = sys.argv[1:]
with open({str(log_path)!r}, "a", encoding="utf-8") as lf:
    lf.write(json.dumps(argv) + "\\n")

if argv[:2] == ["api", "snapshot"]:
    if cfg["snapshot_rc"] != 0:
        sys.stderr.write(cfg["snapshot_stderr"])
        sys.exit(cfg["snapshot_rc"])
    print(json.dumps(cfg["snapshot"]))
    sys.exit(0)

if argv[:2] == ["pane", "process-info"]:
    pane_id = None
    if "--pane" in argv:
        idx = argv.index("--pane")
        if idx + 1 < len(argv):
            pane_id = argv[idx + 1]
    info = cfg["process_info"].get(pane_id)
    if info is None:
        sys.stderr.write("unknown pane\\n")
        sys.exit(1)
    print(json.dumps(info))
    sys.exit(0)

if argv[:2] == ["pane", "read"]:
    pane_id = argv[2] if len(argv) > 2 else ""
    entry = cfg["pane_read"].get(pane_id)
    if entry is None and "visible" in argv:
        print(visible(pane_id))
        sys.exit(0)
    if entry is None:
        sys.stderr.write("cannot read pane\\n")
        sys.exit(1)
    rc, out, err = entry
    if out:
        sys.stdout.write(out)
    if err:
        sys.stderr.write(err)
    sys.exit(rc)

if len(argv) >= 2 and argv[0] == "pane" and argv[1] in ("send", "send-text", "send-keys", "write", "input"):
    if cfg["pane_send_rc"] != 0:
        sys.stderr.write(cfg["pane_send_stderr"])
        sys.exit(cfg["pane_send_rc"])
    input_text(argv[2], "\\n" if argv[1] == "send-keys" else argv[3])
    sys.exit(0)

sys.stderr.write(f"unexpected herdr args: {{argv!r}}\\n")
sys.exit(1)
"""
        return self.install_script("herdr", body)

    def herdr_calls(self) -> list[list[str]]:
        log_path = self.state_dir / "herdr_calls.jsonl"
        if not log_path.exists():
            return []
        return [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line]

    def install_tmux_stub(
        self,
        *,
        panes: list[dict[str, Any]] | None = None,
        list_rc: int = 0,
        list_stderr: str = "",
        captures: dict[str, tuple[int, str, str]] | None = None,
        capture_identity: str | None = None,
        send_rc: int = 0,
        send_stderr: str = "",
    ) -> Path:
        """Install a fake `tmux` CLI supporting list-panes, capture-pane, send-keys."""
        cfg_path = self.state_dir / "tmux_cfg.json"
        log_path = self.state_dir / "tmux_calls.jsonl"
        cfg = {
            "panes": panes or [],
            "list_rc": list_rc,
            "list_stderr": list_stderr,
            "captures": captures or {},
            "capture_identity": capture_identity,
            "send_rc": send_rc,
            "send_stderr": send_stderr,
        }
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
        body = f"""
import json, re, sys
cfg = json.loads(open({str(cfg_path)!r}, encoding="utf-8").read())
{composer_fixture(cfg_path)}
argv = sys.argv[1:]
with open({str(log_path)!r}, "a", encoding="utf-8") as lf:
    lf.write(json.dumps(argv) + "\\n")

if argv and argv[0] == "list-panes":
    if cfg["list_rc"] != 0:
        sys.stderr.write(cfg["list_stderr"])
        sys.exit(cfg["list_rc"])
    fmt = ""
    if "-F" in argv:
        fmt = argv[argv.index("-F") + 1]
    for p in cfg["panes"]:
        def repl(m):
            key = m.group(1)
            return str(p.get(key, ""))
        print(re.sub(r"#\\{{([^}}]+)\\}}", repl, fmt))
    sys.exit(0)

if argv[:2] == ["if-shell", "-F"]:
    target, condition, command = argv[3:]
    pane = next(p for p in cfg["panes"] if p["pane_id"] == target)
    identity = cfg["capture_identity"] or ":".join(str(pane[k]) for k in ("pid", "start_time", "pane_pid"))
    observed = identity + ":" + str(pane["pane_dead"])
    if condition != "#{{==:#{{pid}}:#{{start_time}}:#{{pane_pid}}:#{{pane_dead}}," + observed + "}}":
        sys.exit(0)
    prefix = "display-message -p capture ; "
    assert command.startswith(prefix)
    print("capture")
    argv = command[len(prefix):].split()

if argv and argv[0] == "capture-pane":
    target = ""
    if "-t" in argv:
        target = argv[argv.index("-t") + 1]
    entry = cfg["captures"].get(target)
    if entry is None and "-S" not in argv:
        print(visible(target))
        sys.exit(0)
    if entry is None:
        sys.stderr.write("can't find pane\\n")
        sys.exit(1)
    rc, out, err = entry
    if out:
        sys.stdout.write(out)
    if err:
        sys.stderr.write(err)
    sys.exit(rc)

if argv and argv[0] in ("send-keys", "load-buffer", "paste-buffer", "delete-buffer", "set-buffer"):
    if cfg["send_rc"] != 0:
        sys.stderr.write(cfg["send_stderr"])
        sys.exit(cfg["send_rc"])
    if argv[0] == "set-buffer" and "-b" in argv:
        buf_name = argv[argv.index("-b") + 1]
        data = argv[4]
        input_text(argv[9], data[:-2] + ";" if data.endswith(chr(92) + ";") else data)
        print(f"{{buf_name}}-pasted")
    sys.exit(0)

sys.stderr.write(f"unexpected tmux args: {{argv!r}}\\n")
sys.exit(1)
"""
        return self.install_script("tmux", body)

    def tmux_calls(self) -> list[list[str]]:
        log_path = self.state_dir / "tmux_calls.jsonl"
        if not log_path.exists():
            return []
        return [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line]

    def make_orca_script_body(
        self,
        *,
        bots: list[dict[str, Any]] | None = None,
        worktrees: list[dict[str, Any]] | None = None,
        chats_by_bot: dict[str, list[dict[str, Any]]] | None = None,
        inbox_messages: list[dict[str, Any]] | None = None,
        bot_list_rc: int = 0,
        bot_list_stderr: str = "",
        worktree_ps_rc: int = 0,
        worktree_ps_stderr: str = "",
        inbox_rc: int = 0,
        inbox_stderr: str = "",
        tag: str = "default",
    ) -> str:
        cfg_path = self.state_dir / f"orca_cfg_{tag}.json"
        log_path = self.state_dir / f"orca_calls_{tag}.jsonl"
        cfg = {
            "bots": bots or [],
            "worktrees": worktrees or [],
            "chats_by_bot": chats_by_bot or {},
            "inbox_messages": inbox_messages or [],
            "bot_list_rc": bot_list_rc,
            "bot_list_stderr": bot_list_stderr,
            "worktree_ps_rc": worktree_ps_rc,
            "worktree_ps_stderr": worktree_ps_stderr,
            "inbox_rc": inbox_rc,
            "inbox_stderr": inbox_stderr,
        }
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
        return f"""
import json, sys
cfg = json.loads(open({str(cfg_path)!r}, encoding="utf-8").read())
argv = sys.argv[1:]
with open({str(log_path)!r}, "a", encoding="utf-8") as lf:
    lf.write(json.dumps(argv) + "\\n")

if argv[:2] == ["bot", "list"]:
    if cfg["bot_list_rc"] != 0:
        sys.stderr.write(cfg["bot_list_stderr"])
        sys.exit(cfg["bot_list_rc"])
    print(json.dumps({{"ok": True, "result": {{"bots": cfg["bots"]}}}}))
    sys.exit(0)

if argv[:2] == ["worktree", "ps"]:
    if cfg["worktree_ps_rc"] != 0:
        sys.stderr.write(cfg["worktree_ps_stderr"])
        sys.exit(cfg["worktree_ps_rc"])
    print(json.dumps({{"ok": True, "result": {{"worktrees": cfg["worktrees"]}}}}))
    sys.exit(0)

if argv[:2] == ["bot", "chats"]:
    bot_name = ""
    if "--bot" in argv:
        bot_name = argv[argv.index("--bot") + 1]
    chats = cfg["chats_by_bot"].get(bot_name, [])
    print(json.dumps({{"ok": True, "result": {{"bot": bot_name, "chats": chats}}}}))
    sys.exit(0)

if argv[:2] == ["orchestration", "inbox"]:
    if cfg["inbox_rc"] != 0:
        sys.stderr.write(cfg["inbox_stderr"])
        sys.exit(cfg["inbox_rc"])
    print(json.dumps({{"ok": True, "result": {{"messages": cfg["inbox_messages"]}}}}))
    sys.exit(0)

sys.stderr.write(f"unexpected orca args: {{argv!r}}\\n")
sys.exit(1)
"""

    def orca_calls(self, tag: str = "default") -> list[list[str]]:
        log_path = self.state_dir / f"orca_calls_{tag}.jsonl"
        if not log_path.exists():
            return []
        return [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line]


def sample_herdr_snapshot() -> dict[str, Any]:
    """Minimal valid Herdr session_snapshot with 2 panes across 2 workspaces."""
    return {
        "id": "cli:api:snapshot",
        "result": {
            "type": "session_snapshot",
            "snapshot": {
                "agents": [
                    {
                        "pane_id": "w1:p1",
                        "kind": "agy",
                        "status": "idle",
                    }
                ],
                "focused_pane_id": "w1:p1",
                "focused_tab_id": "w1:t1",
                "focused_workspace_id": "w1",
                "workspaces": [
                    {
                        "workspace_id": "w1",
                        "label": "my-app",
                        "number": 1,
                        "focused": True,
                        "active_tab_id": "w1:t1",
                        "agent_status": "idle",
                        "pane_count": 1,
                        "tab_count": 1,
                    },
                    {
                        "workspace_id": "w2",
                        "label": "shell-ws",
                        "number": 2,
                        "focused": False,
                        "active_tab_id": "w2:t1",
                        "agent_status": "unknown",
                        "pane_count": 1,
                        "tab_count": 1,
                    },
                ],
                "tabs": [
                    {
                        "tab_id": "w1:t1",
                        "workspace_id": "w1",
                        "label": "1",
                        "number": 1,
                        "focused": True,
                        "pane_count": 1,
                        "agent_status": "idle",
                    },
                    {
                        "tab_id": "w2:t1",
                        "workspace_id": "w2",
                        "label": "1",
                        "number": 1,
                        "focused": True,
                        "pane_count": 1,
                        "agent_status": "unknown",
                    },
                ],
                "panes": [
                    {
                        "pane_id": "w1:p1",
                        "tab_id": "w1:t1",
                        "workspace_id": "w1",
                        "cwd": "/home/tester/work/my-app",
                        "foreground_cwd": "/home/tester/work/my-app",
                        "focused": True,
                        "agent_status": "idle",
                        "terminal_id": "term_1",
                        "terminal_title": "agy",
                        "terminal_title_stripped": "agy",
                    },
                    {
                        "pane_id": "w2:p1",
                        "tab_id": "w2:t1",
                        "workspace_id": "w2",
                        "cwd": "/home/tester/work/shell-ws",
                        "foreground_cwd": "/home/tester/work/shell-ws",
                        "focused": False,
                        "agent_status": "unknown",
                        "terminal_id": "term_2",
                        "terminal_title": "bash",
                        "terminal_title_stripped": "bash",
                    },
                ],
                "layouts": [],
            },
        },
    }


def sample_herdr_process_info() -> dict[str, dict[str, Any]]:
    return {
        "w1:p1": {
            "id": "cli:pane:process_info",
            "result": {
                "type": "pane_process_info",
                "process_info": {
                    "pane_id": "w1:p1",
                    "shell_pid": 1000,
                    "foreground_process_group_id": 1001,
                    "foreground_processes": [
                        {
                            "pid": 1001,
                            "name": "agy",
                            "cmdline": "agy --chat",
                            "argv": ["agy", "--chat"],
                            "cwd": "/home/tester/work/my-app",
                        }
                    ],
                },
            },
        },
        "w2:p1": {
            "id": "cli:pane:process_info",
            "result": {
                "type": "pane_process_info",
                "process_info": {
                    "pane_id": "w2:p1",
                    "shell_pid": 2000,
                    "foreground_process_group_id": 2000,
                    "foreground_processes": [
                        {
                            "pid": 2000,
                            "name": "bash",
                            "cmdline": "/usr/bin/bash",
                            "argv": ["/usr/bin/bash"],
                            "cwd": "/home/tester/work/shell-ws",
                        }
                    ],
                },
            },
        },
    }
