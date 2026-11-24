"""Independent spec-based live E2E tests against real Herdr, tmux, and Orca."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

from tests.helpers import (
    FakeEnvironment,
    ISO_UTC_RE,
    LIST_SCHEMA_KEYS,
    READ_SCHEMA_KEYS,
    clean_base_env,
    run_cli,
)

REQUIRE_ALL = os.environ.get("E2E_REQUIRE_ALL") == "1"


def _real_orca_candidates() -> tuple[Path, Path]:
    xdg = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    shim = xdg / "orca" / "linux-orca-cli-shim" / "orca"
    appimage = Path.home() / ".local" / "opt" / "orca" / "orca.AppImage"
    return shim, appimage


class IsolatedTmuxServer:
    """Manages a disposable tmux server under a private TMUX_TMPDIR."""

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="agent-sessions-e2e-tmux-")
        self.tmpdir = self._tmp.name
        self.env = clean_base_env({"TMUX_TMPDIR": self.tmpdir})

    def tmux(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["tmux", "-f", "/dev/null", *args],
            env=self.env,
            capture_output=True,
            text=True,
            check=check,
            timeout=15.0,
        )

    def __enter__(self) -> "IsolatedTmuxServer":
        return self

    def __exit__(self, *exc: Any) -> None:
        try:
            self.tmux("kill-server", check=False)
        finally:
            self._tmp.cleanup()


class LiveE2ESpecTests(unittest.TestCase):
    def _require_or_skip(self, condition: bool, reason: str) -> None:
        if not condition:
            if REQUIRE_ALL:
                self.fail(f"Prerequisite missing under E2E_REQUIRE_ALL=1: {reason}")
            self.skipTest(reason)

    def _live_herdr_panes(self) -> list[dict[str, Any]]:
        res = run_cli(["list", "--source", "herdr", "--format", "json"], env=clean_base_env())
        if res.returncode != 0:
            return []
        return json.loads(res.stdout)

    def _live_orca_bots(self) -> list[dict[str, Any]]:
        res = run_cli(["list", "--source", "orca", "--format", "json"], env=clean_base_env())
        if res.returncode != 0:
            return []
        return json.loads(res.stdout)

    def test_E2E_01_live_list_all_sources_and_formats(self) -> None:
        self._require_or_skip(shutil.which("herdr") is not None, "herdr not installed")
        self._require_or_skip(shutil.which("tmux") is not None, "tmux not installed")

        with IsolatedTmuxServer() as srv:
            srv.tmux("new-session", "-d", "-s", "e2e-list", "-c", "/tmp", "cat")
            time.sleep(0.2)

            r_json = run_cli(["list", "--source", "all", "--format", "json"], env=srv.env)
            self.assertEqual(r_json.returncode, 0, r_json.stderr)
            rows = json.loads(r_json.stdout)
            sources = [r["source"] for r in rows]
            self.assertIn("herdr", sources)
            self.assertIn("tmux", sources)
            self.assertIn("orca", sources)
            # Order: herdr -> tmux -> orca
            self.assertLess(sources.index("herdr"), sources.index("tmux"))
            self.assertLess(sources.index("tmux"), sources.index("orca"))
            for row in rows:
                self.assertEqual(set(row.keys()), LIST_SCHEMA_KEYS)

            # Table format, --no-header, --width, --agents-only
            r_tbl = run_cli(["list", "--source", "all", "--format", "table"], env=srv.env)
            self.assertEqual(r_tbl.returncode, 0, r_tbl.stderr)
            self.assertTrue(r_tbl.stdout.splitlines()[0].startswith("SOURCE"))

            r_no_hdr = run_cli(["list", "--source", "tmux", "--no-header"], env=srv.env)
            self.assertEqual(r_no_hdr.returncode, 0)
            self.assertNotIn("SOURCE", r_no_hdr.stdout)
            self.assertIn("e2e-list", r_no_hdr.stdout)

            r_agents = run_cli(["list", "--source", "all", "--agents-only", "--format", "json"], env=srv.env)
            self.assertEqual(r_agents.returncode, 0)
            agent_rows = json.loads(r_agents.stdout)
            self.assertTrue(any(r["source"] == "orca" for r in agent_rows))
            for r in agent_rows:
                if r["source"] != "orca":
                    self.assertIsNotNone(r["agent"])

    def test_E2E_02_live_orca_discovery_without_path_and_fallback(self) -> None:
        shim, appimage = _real_orca_candidates()
        self._require_or_skip(shim.exists(), f"Orca shim missing at {shim}")
        self._require_or_skip(appimage.exists(), f"Orca AppImage missing at {appimage}")

        # 1) Minimal PATH (no orca on PATH, decoy orca on PATH ignored)
        with FakeEnvironment() as fake:
            fake.install_script(
                "orca",
                fake.make_orca_script_body(
                    bots=[{"id": "decoy", "name": "decoy-path-bot", "agent": "codex", "paused": False}],
                    tag="decoy",
                ),
            )
            # Put decoy `orca` FIRST on PATH; real shim must still be used and decoy ignored
            env = clean_base_env({"PATH": f"{fake.bin_dir}:{os.environ.get('PATH', '')}"})
            res = run_cli(["list", "--source", "orca", "--format", "json"], env=env)
            self.assertEqual(res.returncode, 0, res.stderr)
            names = [r["session"] for r in json.loads(res.stdout)]
            self.assertNotIn("decoy-path-bot", names)
            self.assertTrue(len(names) >= 1)

        # 2) Stale shim ("Orca CLI is unavailable") falls back to real AppImage
        with FakeEnvironment() as fake:
            fake.install_orca_shim(
                fake.make_orca_script_body(
                    bot_list_rc=1,
                    bot_list_stderr="Orca CLI is unavailable\n",
                    tag="stale",
                ),
                mode=0o755,
            )
            real_app_link = fake.home_dir / ".local" / "opt" / "orca" / "orca.AppImage"
            real_app_link.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(appimage, real_app_link)

            env_fb = fake.env({"PATH": f"{fake.bin_dir}:{os.environ.get('PATH', '')}"})
            res = run_cli(["list", "--source", "orca", "--format", "json"], env=env_fb)
            self.assertEqual(res.returncode, 0, res.stderr)
            bots = json.loads(res.stdout)
            self.assertGreaterEqual(len(bots), 1)

    def test_E2E_03_live_herdr_read_and_safe_dry_run_send(self) -> None:
        panes = self._live_herdr_panes()
        self._require_or_skip(len(panes) > 0, "No live Herdr panes available")
        pane = panes[0]
        target = f"herdr:{pane['session']}/{pane['pane']}"

        r_json = run_cli(["read", "--format", "json", "--limit", "10", target], env=clean_base_env())
        self.assertEqual(r_json.returncode, 0, r_json.stderr)
        recs = json.loads(r_json.stdout)
        self.assertEqual(len(recs), 1)
        rec = recs[0]
        self.assertEqual(set(rec.keys()), READ_SCHEMA_KEYS)
        self.assertEqual(rec["source"], "herdr")
        self.assertFalse(rec["structured"])
        self.assertEqual(rec["type"], "terminal-output")
        self.assertRegex(rec["timestamp"], ISO_UTC_RE)

        r_tbl = run_cli(["read", "--format", "table", "--limit", "5", f"herdr:{pane['pane_id']}"], env=clean_base_env())
        self.assertEqual(r_tbl.returncode, 0, r_tbl.stderr)
        self.assertIn("terminal-output", r_tbl.stdout)
        self.assertNotIn("\x1b", r_tbl.stdout)

        # Safe dry-run send (never delivers input to live Herdr panes)
        r_dry = run_cli(["send", "--dry-run", "--format", "json", target, "status check"], env=clean_base_env())
        self.assertEqual(r_dry.returncode, 0, r_dry.stderr)
        dry = json.loads(r_dry.stdout)
        self.assertFalse(dry["sent"])
        self.assertTrue(dry["dry_run"])
        self.assertEqual(dry["target"]["pane_id"], pane["pane_id"])

    def test_E2E_04_live_tmux_isolated_read_send_dead_pane_loop(self) -> None:
        self._require_or_skip(shutil.which("tmux") is not None, "tmux not installed")

        with IsolatedTmuxServer() as srv:
            # 1) Live pane with ANSI output + cat
            srv.tmux(
                "new-session",
                "-d",
                "-s",
                "e2e-loop",
                "-c",
                "/tmp",
                "sh",
                "-c",
                "printf 'READY-1\\n\\033[32mCOLOR-OK\\033[0m\\nREADY-2\\n'; exec cat",
            )
            time.sleep(0.3)

            # Read JSON & table
            r_json = run_cli(["read", "--format", "json", "--limit", "10", "tmux:e2e-loop"], env=srv.env)
            self.assertEqual(r_json.returncode, 0, r_json.stderr)
            recs = json.loads(r_json.stdout)
            self.assertEqual(len(recs), 1)
            self.assertEqual(set(recs[0].keys()), READ_SCHEMA_KEYS)
            self.assertFalse(recs[0]["structured"])
            self.assertEqual(recs[0]["type"], "terminal-output")
            self.assertIn("READY-1", recs[0]["body"])
            self.assertIn("COLOR-OK", recs[0]["body"])

            r_tbl = run_cli(["read", "--format", "table", "--limit", "10", "tmux:e2e-loop"], env=srv.env)
            self.assertEqual(r_tbl.returncode, 0, r_tbl.stderr)
            self.assertNotIn("\x1b", r_tbl.stdout)
            self.assertIn("COLOR-OK", r_tbl.stdout)

            # Live send with Enter, then read back to verify delivery
            r_send = run_cli(["send", "--format", "json", "tmux:e2e-loop", "E2E-PING", "123"], env=srv.env)
            self.assertEqual(r_send.returncode, 0, r_send.stderr)
            send_data = json.loads(r_send.stdout)
            self.assertTrue(send_data["sent"])
            self.assertTrue(send_data["enter"])
            time.sleep(0.3)

            r_after = run_cli(["read", "--format", "json", "--limit", "20", "tmux:e2e-loop"], env=srv.env)
            self.assertEqual(r_after.returncode, 0, r_after.stderr)
            self.assertIn("E2E-PING 123", json.loads(r_after.stdout)[0]["body"])

            # Live send with --no-enter and -- '--flag'
            r_no_enter = run_cli(
                ["send", "--no-enter", "tmux:e2e-loop", "--", "--flag-only"],
                env=srv.env,
            )
            self.assertEqual(r_no_enter.returncode, 0, r_no_enter.stderr)
            time.sleep(0.2)
            r_after2 = run_cli(["read", "--format", "json", "--limit", "20", "tmux:e2e-loop"], env=srv.env)
            self.assertIn("--flag-only", json.loads(r_after2.stdout)[0]["body"])

            # 2) Dead pane: readable by `read` (exit 0), rejected by `send` (exit 4)
            srv.tmux("set-option", "-g", "remain-on-exit", "on")
            srv.tmux("new-session", "-d", "-s", "e2e-dead", "-c", "/tmp", "sh", "-c", "echo DEAD-OUTPUT; exit 0")
            time.sleep(0.4)

            r_dead_read = run_cli(["read", "--format", "json", "tmux:e2e-dead:0.0"], env=srv.env)
            self.assertEqual(r_dead_read.returncode, 0, r_dead_read.stderr)
            self.assertIn("DEAD-OUTPUT", json.loads(r_dead_read.stdout)[0]["body"])

            r_dead_send = run_cli(["send", "tmux:e2e-dead:0.0", "hello"], env=srv.env)
            self.assertEqual(r_dead_send.returncode, 4, r_dead_send.stderr)

    def test_E2E_05_live_orca_read_schema_and_read_flags_unchanged(self) -> None:
        bots = self._live_orca_bots()
        bot_names = [b["session"] for b in bots]
        self._require_or_skip("claude" in bot_names, "Orca bot 'claude' not present")

        shim, _ = _real_orca_candidates()
        self._require_or_skip(shim.exists(), "Orca shim missing")

        def fetch_read_flags() -> dict[str, Any]:
            raw = subprocess.run(
                [str(shim), "orchestration", "inbox", "--json", "--full", "--limit", "1000"],
                capture_output=True,
                text=True,
                check=True,
                timeout=20.0,
            )
            msgs = json.loads(raw.stdout)["result"]["messages"]
            return {m["id"]: m.get("read") for m in msgs}

        before_flags = fetch_read_flags()

        r_json = run_cli(["read", "--format", "json", "--limit", "5", "orca:claude"], env=clean_base_env())
        self.assertEqual(r_json.returncode, 0, r_json.stderr)
        recs = json.loads(r_json.stdout)
        self._require_or_skip(len(recs) > 0, "Orca bot 'claude' has no inbox messages")
        self.assertLessEqual(len(recs), 5)

        timestamps = []
        for rec in recs:
            self.assertEqual(set(rec.keys()), READ_SCHEMA_KEYS)
            self.assertEqual(rec["source"], "orca")
            self.assertEqual(rec["target"], "orca:claude")
            self.assertTrue(rec["structured"])
            self.assertNotEqual(rec["type"], "terminal-output")
            self.assertIsNotNone(rec["id"])
            self.assertRegex(rec["timestamp"], ISO_UTC_RE)
            timestamps.append(rec["timestamp"])

        # Chronological order (non-decreasing timestamps)
        self.assertEqual(timestamps, sorted(timestamps))

        # Verify read flags were NOT mutated by `agent-sessions read`
        after_flags = fetch_read_flags()
        for rec in recs:
            mid = rec["id"]
            if mid in before_flags and mid in after_flags:
                self.assertEqual(
                    before_flags[mid],
                    after_flags[mid],
                    f"Message {mid} read flag changed from {before_flags[mid]} to {after_flags[mid]}",
                )

        # Table format also succeeds
        r_tbl = run_cli(["read", "--format", "table", "--limit", "2", "orca:claude"], env=clean_base_env())
        self.assertEqual(r_tbl.returncode, 0, r_tbl.stderr)
        self.assertTrue(r_tbl.stdout.strip())

    def test_E2E_06_live_error_cases(self) -> None:
        # Missing target -> exit 4
        r4 = run_cli(["read", "orca:definitely-no-such-bot-xyz"], env=clean_base_env())
        self.assertEqual(r4.returncode, 4, r4.stderr)

        # Unavailable source -> exit 3 (empty PATH without herdr)
        with FakeEnvironment() as fake:
            r3 = run_cli(["read", "herdr:my-app/1"], env=fake.env())
            self.assertEqual(r3.returncode, 3, r3.stderr)

        # Invalid arguments -> exit 2
        r2 = run_cli(["read", "--limit", "0", "orca:claude"], env=clean_base_env())
        self.assertEqual(r2.returncode, 2, r2.stderr)


if __name__ == "__main__":
    unittest.main()
