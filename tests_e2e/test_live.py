"""End-to-end checks of the repository launcher against local tools."""
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
SESSION = "e2e-日本語"
APPIMAGE = Path.home() / ".local/opt/orca/orca.AppImage"


class LiveTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="agent-sessions-e2e-")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.env = dict(os.environ, TMUX_TMPDIR=tmp.name)
        for name in ("TMUX", "TMUX_PANE"):
            self.env.pop(name, None)

    def unavailable(self, reason):
        if os.environ.get("E2E_REQUIRE_ALL") == "1":
            self.fail(reason)
        self.skipTest(reason)

    def command(self, *argv, env=None, timeout=35):
        return subprocess.run(argv, cwd=ROOT, env=self.env if env is None else env, text=True,
                              capture_output=True, timeout=timeout)

    def cli(self, *args, env=None):
        result = self.command("./agent-sessions", *args, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def reference(self, *argv):
        try:
            result = self.command(*argv, timeout=15)
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.unavailable(f"{argv[0]} unavailable: {exc}")
        if result.returncode:
            self.unavailable(f"{argv[0]} returned {result.returncode}: {result.stderr.strip()}")
        try:
            return json.loads(result.stdout)
        except ValueError:
            self.unavailable(f"{argv[0]} did not return a JSON reference")

    def snapshot(self):
        data = self.reference("herdr", "api", "snapshot")["result"]["snapshot"]
        if not data["panes"]:
            self.unavailable("Herdr has no panes")
        return data

    def bots(self):
        image = APPIMAGE
        try:
            info = image.stat()
        except OSError:
            self.unavailable("known Orca AppImage is missing")
        if (not stat.S_ISREG(info.st_mode) or info.st_uid not in (0, os.geteuid())
                or info.st_mode & 0o022 or not os.access(image, os.X_OK)):
            self.unavailable("known Orca AppImage is not eligible for execution")
        data = self.reference(str(image), "bot", "list", "--json")
        if not isinstance(data, dict) or data.get("ok") is not True:
            self.unavailable("Orca bot list unavailable")
        result = data.get("result")
        bots = result.get("bots") if isinstance(result, dict) else None
        if not isinstance(bots, list) or not all(isinstance(b, dict) and isinstance(b.get("name"), str) for b in bots):
            self.unavailable("Orca bot list is malformed")
        if not bots:
            self.unavailable("Orca has no bots")
        return bots

    def tmux(self, *args):
        result = self.command("tmux", *args)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def start_tmux(self):
        if not shutil.which("tmux"):
            self.unavailable("tmux is not installed")
        self.tmux("-f", "/dev/null", "new-session", "-d", "-s", SESSION, "cat")
        self.addCleanup(self.tmux, "kill-server")

    def test_E1_herdr_snapshot(self):
        snap = self.snapshot()
        labels = {w["workspace_id"]: w.get("label") or w["workspace_id"] for w in snap["workspaces"]}
        rows = json.loads(self.cli("--source", "herdr", "--format", "json"))
        expected = [(p["pane_id"], labels.get(p["workspace_id"], p["workspace_id"])) for p in snap["panes"]]
        self.assertCountEqual([(r["pane_id"], r["session"]) for r in rows], expected)

    def test_E2_tmux_session(self):
        self.start_tmux()
        self.tmux("split-window", "-d", "-t", SESSION, "cat")
        name = self.tmux("list-sessions", "-F", "#{session_name}")
        ids = self.tmux("list-panes", "-t", SESSION, "-F", "#{pane_id}").splitlines()
        self.assertEqual((name, len(ids)), (SESSION, 2))
        rows = json.loads(self.cli("--source", "tmux", "--format", "json"))
        self.assertCountEqual([(r["session"], r["pane_id"]) for r in rows], [(name, pid) for pid in ids])

    def test_E3_orca_without_path(self):
        bots = self.bots()
        env = dict(self.env, PATH="/usr/bin:/bin")
        for name in ("ORCA_CLI_COMMAND", "AGENT_SESSIONS_ORCA"):
            env.pop(name, None)
        rows = json.loads(self.cli("--source", "orca", "--format", "json", env=env))
        self.assertCountEqual([r["session"] for r in rows], [b["name"] for b in bots])

    def check_appimage_route(self, *, stale_shim):
        bots = self.bots()
        config = self.tmp / "config"
        config.mkdir()
        log = self.tmp / "launches.jsonl"
        env = dict(self.env, PATH="/usr/bin:/bin", XDG_CONFIG_HOME=str(config),
                   PYTHONPATH=str(ROOT / "tests_e2e/trace"), AGENT_SESSIONS_E2E_EXEC_LOG=str(log))
        for name in ("AGENT_SESSIONS_ORCA", "ORCA_CLI_COMMAND"):
            env.pop(name, None)

        expected = []
        if stale_shim:
            shim = config / "orca/linux-orca-cli-shim/orca"
            shim.parent.mkdir(parents=True)
            shim.write_text("#!/bin/sh\nprintf 'Orca CLI is unavailable\\n' >&2\nexit 1\n")
            shim.chmod(0o700)
            expected.append([str(shim), "bot", "list", "--json"])
        expected.extend([[str(APPIMAGE), "bot", "list", "--json"],
                         [str(APPIMAGE), "worktree", "ps", "--json"]])

        rows = json.loads(self.cli("--source", "orca", "--format", "json", env=env))
        self.assertTrue(log.is_file(), "the CLI did not record any launches")
        launches = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertEqual(launches, [[argv[0], argv] for argv in expected])
        self.assertCountEqual([r["session"] for r in rows], [b["name"] for b in bots])

    def test_E3b_orca_appimage_only(self):
        self.check_appimage_route(stale_shim=False)

    def test_E3c_orca_stale_shim_falls_back_to_appimage(self):
        self.check_appimage_route(stale_shim=True)

    def test_E4_orca_bot_details(self):
        bots = self.bots()
        rows = json.loads(self.cli("--source", "orca", "--format", "json"))
        self.assertCountEqual([(r["session"], r["agent"], r["status"] == "paused") for r in rows],
                              [(b["name"], b["agent"], b["paused"]) for b in bots])
        for r in rows:
            self.assertIn(r["status"], {"paused", "working", "blocked", "idle", "stopped", "unknown"})

    def test_E5_all_sources(self):
        self.snapshot()
        self.bots()
        self.start_tmux()
        rows = json.loads(self.cli("--format", "json"))
        table = self.cli("--width", "200").splitlines()
        for sources in ([r["source"] for r in rows], [line.split()[0] for line in table[1:]]):
            self.assertEqual(list(dict.fromkeys(sources)), ["herdr", "tmux", "orca"])
            self.assertEqual(sources, sorted(sources, key=["herdr", "tmux", "orca"].index))

    def test_E6_dry_run(self):
        snap = self.snapshot()
        self.start_tmux()
        pid = snap["panes"][0]["pane_id"]
        # Use the public pane label, including any disambiguating suffix.
        rows = json.loads(self.cli("--source", "herdr", "--format", "json"))
        row = next(r for r in rows if r["pane_id"] == pid)
        for target in (f"herdr:{pid}", f"herdr:{row['session']}/{row['pane']}"):
            preview = self.cli("send", "--dry-run", target, "e2e preview")
            self.assertTrue(preview.startswith("Would send to herdr "))
            self.assertIn(f"({pid})", preview)
        active = self.tmux("display-message", "-p", "-t", SESSION, "#{pane_id}")
        preview = self.cli("send", "--dry-run", f"tmux:{SESSION}", "e2e preview")
        self.assertTrue(preview.startswith("Would send to tmux "))
        self.assertIn(f"({active})", preview)

    def test_E7_ignore_unknown_path_orca(self):
        marker = self.tmp / "invoked"
        fake = self.tmp / "orca"
        fake.write_text('#!/bin/sh\n: > "$E2E_MARKER"\n')
        fake.chmod(0o700)
        env = dict(self.env, HOME=str(self.tmp), XDG_CONFIG_HOME=str(self.tmp),
                   PATH=f"{self.tmp}:{os.defpath}", E2E_MARKER=str(marker))
        for name in ("ORCA_CLI_COMMAND", "AGENT_SESSIONS_ORCA"):
            env.pop(name, None)
        self.assertEqual(json.loads(self.cli("--source", "orca", "--format", "json", env=env)), [])
        self.assertFalse(marker.exists())

    def test_E8_tmux_send(self):
        self.start_tmux()
        pid = self.tmux("display-message", "-p", "-t", SESSION, "#{pane_id}")
        message = "e2e harmless text"
        receipt = json.loads(self.cli("send", "--format", "json", f"tmux:{pid}", message))
        self.assertTrue(receipt["sent"])
        self.assertEqual(receipt["target"]["pane_id"], pid)
        deadline = time.monotonic() + 2
        while self.tmux("capture-pane", "-p", "-t", pid).splitlines().count(message) < 2:
            if time.monotonic() > deadline:
                self.fail("cat did not receive and echo the submitted line")
            time.sleep(0.02)
