"""Live comparisons with the source CLIs and platform-specific process discovery."""

import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from tests.helpers import READ_SCHEMA_KEYS, REPO_ROOT as ROOT, clean_base_env, run_cli

SESSION = "test-processes"
APPIMAGE = Path.home() / ".local/opt/orca/orca.AppImage"
MAC_BUNDLE = Path("Applications/Orca.app/Contents/Resources/bin/orca")


class LiveReferenceTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(
            prefix="as-e2e-", dir="/tmp" if platform.system() == "Darwin" else None
        )
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.env = clean_base_env({"TMUX_TMPDIR": tmp.name})

    def unavailable(self, reason):
        if os.environ.get("E2E_REQUIRE_ALL") == "1":
            self.fail(reason)
        self.skipTest(reason)

    def command(self, *argv, env=None, timeout=35, text=True):
        return subprocess.run(
            argv,
            cwd=ROOT,
            env=self.env if env is None else env,
            text=text,
            capture_output=True,
            timeout=timeout,
        )

    def cli(self, *args, env=None, observe=False):
        result = run_cli(list(args), env=self.env if env is None else env,
                         instrumentation=ROOT / "tests_e2e/observe" if observe else None)
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
        paths = (
            [Path("/") / MAC_BUNDLE, Path.home() / MAC_BUNDLE]
            if platform.system() == "Darwin"
            else [APPIMAGE]
        )
        for path in paths:
            try:
                info = path.stat()
            except OSError:
                continue
            if (
                stat.S_ISREG(info.st_mode)
                and info.st_uid in (0, os.geteuid())
                and not info.st_mode & 0o022
                and os.access(path, os.X_OK)
            ):
                return path
        self.unavailable("no eligible Orca application at a known location")

    def bots(self, *, allow_empty=False, binary=None):
        data = self.reference(str(binary or self.orca_binary()), "bot", "list", "--json")
        if not isinstance(data, dict) or data.get("ok") is not True:
            self.unavailable("Orca bot list unavailable")
        result = data.get("result")
        bots = result.get("bots") if isinstance(result, dict) else None
        if not isinstance(bots, list) or not all(
            isinstance(b, dict) and isinstance(b.get("name"), str) for b in bots
        ):
            self.unavailable("Orca bot list is malformed")
        if not bots and not allow_empty:
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

    def check_appimage_route(self, directory, *, stale_shim):
        bots = self.bots()
        config = directory / "config"
        config.mkdir()
        log = directory / "launches.jsonl"
        env = dict(
            self.env,
            PATH="/usr/bin:/bin",
            XDG_CONFIG_HOME=str(config),
            AGENT_SESSIONS_E2E_EXEC_LOG=str(log),
        )
        for name in ("AGENT_SESSIONS_ORCA", "ORCA_CLI_COMMAND"):
            env.pop(name, None)

        expected = []
        if stale_shim:
            shim = config / "orca/linux-orca-cli-shim/orca"
            shim.parent.mkdir(parents=True)
            shim.write_text("#!/bin/sh\nprintf 'Orca CLI is unavailable\\n' >&2\nexit 1\n")
            shim.chmod(0o700)
            expected.append([str(shim), "bot", "list", "--json"])
        expected.extend(
            [[str(APPIMAGE), "bot", "list", "--json"], [str(APPIMAGE), "worktree", "ps", "--json"]]
        )

        started = time.monotonic()
        result = run_cli(["--source", "orca", "--format", "json"], env=env, instrumentation=ROOT / "tests_e2e/observe")
        elapsed = time.monotonic() - started
        launches = (
            [json.loads(line) for line in log.read_text().splitlines()] if log.is_file() else []
        )
        # Orca failures are silent by design, so a failure here must carry its own evidence.
        diagnostics = (
            f"\nexit={result.returncode} elapsed={elapsed:.1f}s"
            f"\nlaunch count={len(launches)}"
            f"\nlaunches={launches!r}"
        )
        self.assertEqual(result.returncode, 0, diagnostics)
        rows = json.loads(result.stdout)
        self.assertTrue(log.is_file(), "the CLI did not record any launches" + diagnostics)
        self.assertEqual(launches, [["launch", argv[0], argv] for argv in expected], diagnostics)
        self.assertCountEqual([r["session"] for r in rows], [b["name"] for b in bots], diagnostics)

    @unittest.skipUnless(platform.system() in ("Darwin", "Linux"), "requires macOS or Linux")
    def test_platform_process_routes(self):
        ps = ["ps", "-A", "-ww", "-o", "pid=,ppid=,args="]
        if not shutil.which("ps"):
            self.unavailable("ps is not installed")
        # Check host support separately from the CLI; never log the process table.
        try:
            probe = subprocess.run(
                ps, env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.unavailable(f"ps unavailable: {exc}")
        if probe.returncode:
            self.unavailable("ps does not support the required process snapshot")
        self.start_tmux()
        sleeper = self.tmux(
            "split-window",
            "-d",
            "-P",
            "-F",
            "#{pane_id}",
            "-t",
            SESSION,
            "sh",
            "-c",
            "sleep 304; :",
        )
        pane_pid = self.tmux("display-message", "-p", "-t", sleeper, "#{pane_pid}")
        panes = json.loads(self.cli("--source", "tmux", "--format", "json"))
        expected = self.tmux("list-panes", "-t", SESSION, "-F", "#{pane_id}").splitlines()
        self.assertCountEqual(
            [(r["session"], r["pane_id"]) for r in panes], [(SESSION, pid) for pid in expected]
        )
        self.assertEqual(len(expected), 2)
        active = self.tmux("display-message", "-p", "-t", SESSION, "#{pane_id}")
        preview = self.cli("send", "--dry-run", "tmux:" + SESSION, "test preview")
        self.assertIn(f"({active})", preview)
        (terminal,) = json.loads(self.cli("read", "--format", "json", "tmux:" + active))
        self.assertEqual(set(terminal), READ_SCHEMA_KEYS)
        log = self.tmp / "launches.jsonl"

        def snapshot(system):
            simulate_darwin = system == "Darwin" and platform.system() == "Linux"
            env = dict(
                self.env,
                AGENT_SESSIONS_E2E_EXEC_LOG=str(log),
                AGENT_SESSIONS_E2E_FAKE_DARWIN="1" if simulate_darwin else "0",
            )
            log.write_text("")  # Each CLI invocation has its own observation window.
            rows = json.loads(self.cli("--source", "tmux", "--format", "json", env=env, observe=True))
            events = [json.loads(line) for line in log.read_text().splitlines()]
            launches = [(e[1], e[2]) for e in events if e[0] == "launch"]
            self.assertTrue(launches)
            self.assertEqual((launches[0][0], launches[0][1][:2]), ("tmux", ["tmux", "list-panes"]))
            reads = [
                e[1] for e in events if e[0] == "proc" and e[1].endswith(("/children", "/cmdline"))
            ]
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
            self.assertEqual(
                [(r["pane_id"], r["command"], r["agent"], r["status"]) for r in rows],
                [(r["pane_id"], r["command"], r["agent"], r["status"]) for r in native],
            )

    @unittest.skipUnless(platform.system() == "Darwin", "not macOS")
    def test_mac_bundle_launches(self):
        binary = self.orca_binary()
        bots = self.bots(binary=binary, allow_empty=True)
        home = Path.home()
        if (home / ".local/bin/orca").exists():
            # Hide the registered shim but keep access to the application and its state.
            home = self.tmp / "home"
            home.mkdir()
            for name in ("Applications", "Library", ".orca", ".config"):
                (home / name).symlink_to(Path.home() / name, target_is_directory=True)
        log = self.tmp / "launches.jsonl"
        env = dict(
            self.discovery_env(),
            HOME=str(home),
            AGENT_SESSIONS_E2E_EXEC_LOG=str(log),
        )
        # Orca alone also returns [] and exits 0 on failure. With the other
        # sources unavailable, "all" exits 0 only if it read a valid bot list.
        for name in ("herdr", "tmux"):
            stub = self.tmp / name
            stub.write_text("#!/bin/sh\nexit 1\n")
            stub.chmod(0o700)
        env["PATH"] = f"{self.tmp}:{env['PATH']}"
        rows = json.loads(self.cli("--source", "all", "--format", "json", env=env, observe=True))
        self.assertCountEqual([r["session"] for r in rows], [b["name"] for b in bots])
        expected = str(binary if binary == Path("/") / MAC_BUNDLE else home / MAC_BUNDLE)
        # The state query runs only when there are bots (same rule on every OS).
        queries = [("bot", "list")] + ([("worktree", "ps")] if bots else [])
        launches = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertEqual([exe for _, exe, _ in launches[:2]], ["herdr", "tmux"])
        self.assertEqual(
            launches[2:], [["launch", expected, [expected, *query, "--json"]] for query in queries]
        )

    def test_decoy_never_runs(self):
        marker = self.tmp / "invoked"
        fake = self.tmp / "orca"
        fake.write_text('#!/bin/sh\n: > "$E2E_MARKER"\n')
        fake.chmod(0o700)
        env = dict(
            self.discovery_env(),
            HOME=str(self.tmp),
            XDG_CONFIG_HOME=str(self.tmp),
            E2E_MARKER=str(marker),
        )
        env["PATH"] = f"{self.tmp}:{env['PATH']}"
        rows = json.loads(self.cli("--source", "orca", "--format", "json", env=env))
        if platform.system() != "Darwin":
            self.assertEqual(rows, [])  # Darwin can still discover a system-wide bundle.
        self.assertFalse(marker.exists())

    def test_real_table_order(self):
        self.snapshot()
        self.bots()
        self.start_tmux()
        rows = json.loads(self.cli("--format", "json"))
        table = self.cli("--width", "200").splitlines()
        for sources in ([r["source"] for r in rows], [line.split()[0] for line in table[1:]]):
            self.assertEqual(list(dict.fromkeys(sources)), ["herdr", "tmux", "orca"])
            self.assertEqual(sources, sorted(sources, key=["herdr", "tmux", "orca"].index))

    def test_live_missing_targets(self):
        self.start_tmux()
        self.snapshot()
        self.bots()
        for target in ("orca:no-such-bot", "tmux:no-such", "herdr:no-such/1"):
            with self.subTest(target=target):
                self.assertEqual(self.command("./agent-sessions", "read", target).returncode, 4)
        env = dict(self.env, AGENT_SESSIONS_ORCA=str(self.tmp / "missing-cli"))
        self.assertEqual(
            self.command("./agent-sessions", "read", "orca:claude", env=env).returncode, 3
        )
        for name in ("herdr", "tmux"):
            stub = self.tmp / name
            stub.write_text("#!/bin/sh\nexit 1\n")
            stub.chmod(0o700)
        env["PATH"] = str(self.tmp) + os.pathsep + env["PATH"]
        self.assertEqual(self.command("./agent-sessions", "read", "no-such", env=env).returncode, 3)

    def test_mailbox_flags_by_id(self):
        binary = self.orca_binary()

        def flags():
            inbox = self.reference(
                str(binary), "orchestration", "inbox", "--json", "--full", "--limit", "1000"
            )
            return {m["id"]: m["read"] for m in inbox["result"]["messages"]}

        if "claude" not in [b["name"] for b in self.bots(binary=binary)]:
            self.unavailable("read E2E requires an Orca bot named claude")
        before = flags()
        rows = json.loads(self.cli("read", "--format", "json", "orca:claude"))
        limited = json.loads(self.cli("read", "--format", "json", "--limit", "3", "orca:claude"))
        after = flags()
        self.assertTrue(rows and limited, "expected mail for orca:claude")
        self.assertLessEqual(len(limited), 3)
        for row in rows + limited:
            self.assertEqual(set(row), READ_SCHEMA_KEYS)
            self.assertIs(row["structured"], True)
            mid = row["id"]
            self.assertTrue(mid in before and mid in after, "message outside comparison window")
            self.assertEqual(before[mid], after[mid], "read flag changed during observation")

    @unittest.skipUnless(platform.system() == "Linux", "not Linux")
    def test_linux_launch_routes(self):
        for stale in (False, True):
            with self.subTest(stale=stale), tempfile.TemporaryDirectory() as directory:
                self.check_appimage_route(Path(directory), stale_shim=stale)

    def test_direct_bot_reference(self):
        bots = self.bots()
        rows = json.loads(
            self.cli("--source", "orca", "--format", "json", env=self.discovery_env())
        )
        self.assertCountEqual(
            [(r["session"], r["agent"], r["status"] == "paused") for r in rows],
            [(b["name"], b["agent"], b["paused"]) for b in bots],
        )
        self.assertTrue(
            all(
                r["status"] in {"paused", "idle", "working", "blocked", "unknown", "stopped"}
                for r in rows
            )
        )

    def test_reference_herdr(self):
        snap = self.snapshot()
        labels = {
            w["workspace_id"]: w.get("label") or w["workspace_id"] for w in snap["workspaces"]
        }
        rows = json.loads(self.cli("--source", "herdr", "--format", "json"))
        self.assertCountEqual(
            [(r["pane_id"], r["session"]) for r in rows],
            [
                (p["pane_id"], labels.get(p["workspace_id"], p["workspace_id"]))
                for p in snap["panes"]
            ],
        )
        for row in rows:
            pid = row["pane_id"]
            target = "herdr:" + pid
            for address in (target, "herdr:" + row["session"] + "/" + row["pane"]):
                result = self.cli("send", "--dry-run", address, "test preview")
                self.assertIn("(" + pid + ")", result)
            direct = self.command(
                "herdr",
                "pane",
                "read",
                pid,
                "--source",
                "recent",
                "--lines",
                "50",
                "--format",
                "text",
                text=False,
            )
            self.assertEqual(direct.returncode, 0)
            last = direct.stdout.rstrip(b"\n").split(b"\n")[-1].decode(errors="replace")
            records = json.loads(self.cli("read", "--format", "json", target))
            self.assertEqual(len(records), 1)
            self.assertEqual(set(records[0]), READ_SCHEMA_KEYS)
            if last and last in records[0]["body"].split("\n"):
                return
        self.fail("no stable reference line")
