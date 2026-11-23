"""End-to-end checks of the repository launcher against local tools."""
import json
import os
import platform
import sys
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
MAC_BUNDLE = Path("Applications/Orca Desktop.app/Contents/MacOS/orca")


class LiveTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="as-e2e-", dir="/tmp" if platform.system() == "Darwin" else None)
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

    def orca_binary(self):
        paths = ([Path("/") / MAC_BUNDLE, Path.home() / MAC_BUNDLE]
                 if platform.system() == "Darwin" else [APPIMAGE])
        for path in paths:
            try:
                info = path.stat()
            except OSError:
                continue
            if (stat.S_ISREG(info.st_mode) and info.st_uid in (0, os.geteuid())
                    and not info.st_mode & 0o022 and os.access(path, os.X_OK)):
                return path
        self.unavailable("no eligible Orca application at a known location")

    def bots(self):
        data = self.reference(str(self.orca_binary()), "bot", "list", "--json")
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
        sleeper = self.tmux("split-window", "-d", "-P", "-F", "#{pane_id}", "-t", SESSION,
                            "sh", "-c", "sleep 300; :")
        name = self.tmux("list-sessions", "-F", "#{session_name}")
        ids = self.tmux("list-panes", "-t", SESSION, "-F", "#{pane_id}").splitlines()
        self.assertEqual((name, len(ids)), (SESSION, 2))
        deadline = time.monotonic() + 2
        while True:
            rows = json.loads(self.cli("--source", "tmux", "--format", "json"))
            self.assertCountEqual([(r["session"], r["pane_id"]) for r in rows], [(name, pid) for pid in ids])
            command = next(r["command"] for r in rows if r["pane_id"] == sleeper)
            if command == "sleep 300":
                break
            if time.monotonic() >= deadline:
                self.fail(f"expected sleep 300 in {sleeper}, got {command!r}")
            time.sleep(0.02)

    def discovery_env(self):
        # Keep the test interpreter even if the system Python is older (e.g. macOS).
        env = dict(self.env, PATH="/usr/bin:/bin")
        if platform.system() == "Darwin":
            tools = self.tmp / "bin"
            tools.mkdir()
            (tools / "python3").symlink_to(sys.executable)
            env["PATH"] = f"{tools}:{env['PATH']}"
        for name in ("ORCA_CLI_COMMAND", "AGENT_SESSIONS_ORCA"):
            env.pop(name, None)
        return env

    def test_E3_orca_without_path(self):
        bots = self.bots()
        env = self.discovery_env()
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

    @unittest.skipUnless(platform.system() == "Linux", "not Linux")
    def test_E3b_orca_appimage_only(self):
        self.check_appimage_route(stale_shim=False)

    @unittest.skipUnless(platform.system() == "Linux", "not Linux")
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
        env = dict(self.discovery_env(), HOME=str(self.tmp), XDG_CONFIG_HOME=str(self.tmp),
                   E2E_MARKER=str(marker))
        env["PATH"] = f"{self.tmp}:{env['PATH']}"
        rows = json.loads(self.cli("--source", "orca", "--format", "json", env=env))
        if platform.system() != "Darwin":
            self.assertEqual(rows, [])  # Darwin can still discover a system-wide bundle.
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

    @unittest.skipUnless(platform.system() in ("Darwin", "Linux"), "requires macOS or Linux")
    def test_E10_macos_ps_path_on_real_process_table(self):
        ps = ["ps", "-A", "-ww", "-o", "pid=,ppid=,args="]
        if not shutil.which("ps"):
            self.unavailable("ps is not installed")
        # Check host support separately from the CLI; never log the process table.
        try:
            probe = subprocess.run(ps, env=self.env, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, timeout=5)
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.unavailable(f"ps unavailable: {exc}")
        if probe.returncode:
            self.unavailable("ps does not support the required process snapshot")
        self.start_tmux()
        sleeper = self.tmux("split-window", "-d", "-P", "-F", "#{pane_id}", "-t", SESSION,
                            "sh", "-c", "sleep 304; :")
        pane_pid = self.tmux("display-message", "-p", "-t", sleeper, "#{pane_pid}")
        log = self.tmp / "launches.jsonl"

        def snapshot(system):
            simulate_darwin = system == "Darwin" and platform.system() == "Linux"
            env = dict(self.env, PYTHONPATH=str(ROOT / "tests_e2e/darwin"),
                       AGENT_SESSIONS_E2E_EXEC_LOG=str(log),
                       AGENT_SESSIONS_E2E_FAKE_DARWIN="1" if simulate_darwin else "0")
            log.write_text("")  # Each CLI invocation has its own observation window.
            rows = json.loads(self.cli("--source", "tmux", "--format", "json", env=env))
            events = [json.loads(line) for line in log.read_text().splitlines()]
            launches = [(e[1], e[2]) for e in events if e[0] == "launch"]
            self.assertTrue(launches)
            self.assertEqual((launches[0][0], launches[0][1][:2]), ("tmux", ["tmux", "list-panes"]))
            reads = [e[1] for e in events if e[0] == "proc" and e[1].endswith(("/children", "/cmdline"))]
            if system == "Darwin":
                self.assertEqual(launches[1:], [("ps", ps)])
                self.assertEqual(reads, [])
            else:
                self.assertEqual(launches[1:], [])
                self.assertIn(f"/proc/{pane_pid}/task/{pane_pid}/children", reads)
            return rows

        deadline = time.monotonic() + 2
        while True:
            rows = snapshot("Darwin")
            command = next(r["command"] for r in rows if r["pane_id"] == sleeper)
            if command == "sleep 304":
                break
            if time.monotonic() >= deadline:
                self.fail(f"ps path: expected 'sleep 304' in {sleeper}, got {command!r}")
            time.sleep(0.02)
        if platform.system() == "Linux":
            native = snapshot("Linux")
            self.assertEqual([(r["pane_id"], r["command"], r["agent"], r["status"]) for r in rows],
                             [(r["pane_id"], r["command"], r["agent"], r["status"]) for r in native])

    @unittest.skipUnless(platform.system() == "Darwin", "not macOS")
    def test_E9_macos_orca_bundle(self):
        binary = self.orca_binary()
        bots = self.bots()
        # Hide the registered user shim while retaining access to app state.
        home = self.tmp / "home"
        home.mkdir()
        for name in ("Applications", "Library", ".orca", ".config"):
            (home / name).symlink_to(Path.home() / name, target_is_directory=True)
        log = self.tmp / "launches.jsonl"
        env = dict(self.discovery_env(), HOME=str(home), PYTHONPATH=str(ROOT / "tests_e2e/trace"),
                   AGENT_SESSIONS_E2E_EXEC_LOG=str(log))
        rows = json.loads(self.cli("--source", "orca", "--format", "json", env=env))
        self.assertCountEqual([r["session"] for r in rows], [b["name"] for b in bots])
        expected = str(binary if binary == Path("/") / MAC_BUNDLE else home / MAC_BUNDLE)
        self.assertEqual([json.loads(line) for line in log.read_text().splitlines()],
                         [[expected, [expected, *query, "--json"]]
                          for query in (("bot", "list"), ("worktree", "ps"))])
