"""Independent spec-based black-box unit tests for `agent-sessions` list command."""

from __future__ import annotations

import json
import unittest

from tests.helpers import (
    FakeEnvironment,
    LIST_SCHEMA_KEYS,
    run_cli,
    sample_herdr_process_info,
    sample_herdr_snapshot,
)


class ListCommandSpecTests(unittest.TestCase):
    def _setup_all_sources(self, fake: FakeEnvironment) -> None:
        fake.install_herdr_stub(
            snapshot=sample_herdr_snapshot(),
            process_info=sample_herdr_process_info(),
        )
        fake.install_tmux_stub(
            panes=[
                {
                    "session_name": "demo",
                    "window_index": "0",
                    "pane_index": "0",
                    "pane_id": "%0",
                    "pane_title": "sleep",
                    "pane_current_command": "sleep",
                    "pane_pid": "999999",
                    "pane_dead": "0",
                    "pane_active": "1",
                    "window_active": "1",
                    "session_attached": "1",
                    "pane_current_path": "/tmp/demo",
                    "pid": "10",
                    "start_time": "100",
                },
                {
                    "session_name": "demo",
                    "window_index": "0",
                    "pane_index": "1",
                    "pane_id": "%1",
                    "pane_title": "bash",
                    "pane_current_command": "bash",
                    "pane_pid": "999998",
                    "pane_dead": "0",
                    "pane_active": "0",
                    "window_active": "1",
                    "session_attached": "1",
                    "pane_current_path": "/tmp/demo",
                    "pid": "10",
                    "start_time": "100",
                },
                {
                    "session_name": "demo",
                    "window_index": "0",
                    "pane_index": "2",
                    "pane_id": "%2",
                    "pane_title": "deadpane",
                    "pane_current_command": "python3",
                    "pane_pid": "999997",
                    "pane_dead": "1",
                    "pane_active": "0",
                    "window_active": "1",
                    "session_attached": "1",
                    "pane_current_path": "/tmp/demo",
                    "pid": "10",
                    "start_time": "100",
                },
            ]
        )
        orca_body = fake.make_orca_script_body(
            bots=[
                {
                    "id": "bot-1",
                    "name": "helper",
                    "agent": "codex",
                    "paused": False,
                    "specialty": "Code review",
                },
                {
                    "id": "bot-2",
                    "name": "snoozer",
                    "agent": "claude",
                    "paused": True,
                    "specialty": None,
                },
                {
                    "id": "bot-3",
                    "name": "offline",
                    "agent": "gemini",
                    "paused": False,
                    "specialty": "Docs",
                },
            ],
            worktrees=[
                {
                    "bot": "helper",
                    "botHome": True,
                    "path": "/home/tester/work/helper",
                    "liveTerminalCount": 1,
                    "status": "idle",
                    "agents": [{"state": "idle"}],
                },
                {
                    "bot": "snoozer",
                    "botHome": True,
                    "path": "/home/tester/work/snoozer",
                    "liveTerminalCount": 1,
                    "status": "idle",
                    "agents": [{"state": "idle"}],
                },
                {
                    "bot": "offline",
                    "botHome": True,
                    "path": "/home/tester/work/offline",
                    "liveTerminalCount": 0,
                    "status": "idle",
                    "agents": [],
                },
            ],
        )
        fake.install_orca_shim(orca_body)

    def test_list_all_sources_order_and_json_schema(self) -> None:
        with FakeEnvironment() as fake:
            self._setup_all_sources(fake)
            res = run_cli(["--format", "json"], env=fake.env())
            self.assertEqual(res.returncode, 0, res.stderr)
            rows = json.loads(res.stdout)
            self.assertIsInstance(rows, list)
            sources = [r["source"] for r in rows]
            # Spec: `all` queries Herdr, tmux, then Orca.
            first_herdr = sources.index("herdr")
            first_tmux = sources.index("tmux")
            first_orca = sources.index("orca")
            self.assertLess(first_herdr, first_tmux)
            self.assertLess(first_tmux, first_orca)
            for row in rows:
                self.assertEqual(set(row.keys()), LIST_SCHEMA_KEYS)
                self.assertIsInstance(row["focused"], bool)
                if row["source"] != "orca":
                    self.assertIsNone(row["specialty"])

    def test_list_subcommand_alias_matches_default(self) -> None:
        with FakeEnvironment() as fake:
            self._setup_all_sources(fake)
            r1 = run_cli(["--format", "json"], env=fake.env())
            r2 = run_cli(["list", "--format", "json"], env=fake.env())
            self.assertEqual(r1.returncode, 0)
            self.assertEqual(r2.returncode, 0)
            self.assertEqual(json.loads(r1.stdout), json.loads(r2.stdout))

    def test_tmux_statuses_running_shell_dead(self) -> None:
        with FakeEnvironment() as fake:
            self._setup_all_sources(fake)
            res = run_cli(["--source", "tmux", "--format", "json"], env=fake.env())
            self.assertEqual(res.returncode, 0, res.stderr)
            by_id = {r["pane_id"]: r for r in json.loads(res.stdout)}
            self.assertEqual(by_id["%0"]["status"], "running")
            self.assertEqual(by_id["%1"]["status"], "shell")
            self.assertEqual(by_id["%2"]["status"], "dead")

    def test_orca_bot_fields_paused_stopped_specialty(self) -> None:
        with FakeEnvironment() as fake:
            self._setup_all_sources(fake)
            res = run_cli(["--source", "orca", "--format", "json"], env=fake.env())
            self.assertEqual(res.returncode, 0, res.stderr)
            by_name = {r["session"]: r for r in json.loads(res.stdout)}
            self.assertEqual(by_name["helper"]["pane"], "home")
            self.assertEqual(by_name["helper"]["command"], "Code review")
            self.assertEqual(by_name["helper"]["specialty"], "Code review")
            self.assertEqual(by_name["helper"]["cwd"], "/home/tester/work/helper")
            self.assertEqual(by_name["snoozer"]["status"], "paused")
            self.assertIsNone(by_name["snoozer"]["specialty"])
            self.assertEqual(by_name["offline"]["status"], "stopped")

    def test_orca_bots_visible_when_worktree_ps_fails(self) -> None:
        with FakeEnvironment() as fake:
            orca_body = fake.make_orca_script_body(
                bots=[
                    {
                        "id": "bot-1",
                        "name": "helper",
                        "agent": "codex",
                        "paused": False,
                        "specialty": "Review",
                    }
                ],
                worktree_ps_rc=1,
                worktree_ps_stderr="worktree ps failed\n",
            )
            fake.install_orca_shim(orca_body)
            res = run_cli(["--source", "orca", "--format", "json"], env=fake.env())
            self.assertEqual(res.returncode, 0, res.stderr)
            rows = json.loads(res.stdout)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["session"], "helper")
            self.assertEqual(rows[0]["pane"], "home")

    def test_agents_only_keeps_identified_agents_and_all_orca_bots(self) -> None:
        with FakeEnvironment() as fake:
            self._setup_all_sources(fake)
            res = run_cli(["--agents-only", "--format", "json"], env=fake.env())
            self.assertEqual(res.returncode, 0, res.stderr)
            rows = json.loads(res.stdout)
            # Non-agent herdr pane (w2:p1) and non-agent tmux panes should be excluded
            for row in rows:
                if row["source"] != "orca":
                    self.assertIsNotNone(row["agent"])
            orca_names = {r["session"] for r in rows if r["source"] == "orca"}
            self.assertEqual(orca_names, {"helper", "snoozer", "offline"})

    def test_table_header_no_header_and_width(self) -> None:
        with FakeEnvironment() as fake:
            self._setup_all_sources(fake)
            res = run_cli(["--source", "orca"], env=fake.env())
            self.assertEqual(res.returncode, 0, res.stderr)
            lines = res.stdout.splitlines()
            self.assertTrue(lines[0].startswith("SOURCE"))
            for col in ("SOURCE", "SESSION", "PANE", "AGENT", "STATUS", "COMMAND", "CWD"):
                self.assertIn(col, lines[0])

            no_hdr = run_cli(["--source", "orca", "--no-header"], env=fake.env())
            self.assertEqual(no_hdr.returncode, 0)
            self.assertEqual(len(no_hdr.stdout.splitlines()), len(lines) - 1)
            self.assertFalse(no_hdr.stdout.startswith("SOURCE"))

            narrow = run_cli(["--source", "orca", "--width", "60"], env=fake.env())
            self.assertEqual(narrow.returncode, 0)
            for line in narrow.stdout.splitlines():
                self.assertLessEqual(len(line), 60)

    def test_missing_tmux_server_counts_as_empty_exit_0(self) -> None:
        with FakeEnvironment() as fake:
            fake.install_tmux_stub(
                list_rc=1,
                list_stderr="no server running on /tmp/tmux-1000/default\n",
            )
            res = run_cli(["--source", "tmux", "--format", "json"], env=fake.env())
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertEqual(json.loads(res.stdout), [])

    def test_orca_only_unreadable_exits_silently_0(self) -> None:
        with FakeEnvironment() as fake:
            # No Orca binary installed anywhere
            res = run_cli(["--source", "orca", "--format", "json"], env=fake.env())
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertEqual(res.stderr, "")
            self.assertEqual(json.loads(res.stdout), [])

    def test_all_sources_fail_exits_3_and_unreadable_orca_not_success(self) -> None:
        with FakeEnvironment() as fake:
            # No herdr, no tmux, no orca installed
            res = run_cli(["--source", "all", "--format", "json"], env=fake.env())
            self.assertEqual(res.returncode, 3)

    def test_invalid_arguments_exit_2(self) -> None:
        with FakeEnvironment() as fake:
            for bad_args in (
                ["--source", "bogus"],
                ["--format", "yaml"],
                ["--width", "0"],
                ["--width", "-5"],
                ["--width", "abc"],
                ["--unknown-flag"],
            ):
                with self.subTest(args=bad_args):
                    res = run_cli(bad_args, env=fake.env())
                    self.assertEqual(res.returncode, 2, f"args={bad_args} stdout={res.stdout} stderr={res.stderr}")

    def test_orca_discovery_env_precedence_and_no_path_search(self) -> None:
        with FakeEnvironment() as fake:
            # Install an `orca` on PATH — must be ignored when env vars and known paths absent
            fake.install_script(
                "orca",
                fake.make_orca_script_body(
                    bots=[{"id": "p", "name": "pathbot", "agent": "codex", "paused": False}],
                    tag="path",
                ),
            )
            res_no_known = run_cli(["--source", "orca", "--format", "json"], env=fake.env())
            self.assertEqual(res_no_known.returncode, 0)
            self.assertEqual(json.loads(res_no_known.stdout), [])

            # AGENT_SESSIONS_ORCA takes precedence over ORCA_CLI_COMMAND
            cli_a = fake.root / "orca_a"
            cli_b = fake.root / "orca_b"
            cli_a.write_text(
                f"#!/usr/bin/env python3\n{fake.make_orca_script_body(bots=[{'id': 'a', 'name': 'bot-a', 'agent': 'codex', 'paused': False}], tag='a')}",
                encoding="utf-8",
            )
            cli_a.chmod(0o755)
            cli_b.write_text(
                f"#!/usr/bin/env python3\n{fake.make_orca_script_body(bots=[{'id': 'b', 'name': 'bot-b', 'agent': 'codex', 'paused': False}], tag='b')}",
                encoding="utf-8",
            )
            cli_b.chmod(0o755)

            env = fake.env({"AGENT_SESSIONS_ORCA": str(cli_a), "ORCA_CLI_COMMAND": str(cli_b)})
            res = run_cli(["--source", "orca", "--format", "json"], env=env)
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertEqual([r["session"] for r in json.loads(res.stdout)], ["bot-a"])

    def test_orca_discovery_rejects_group_or_world_writable_and_falls_back_on_stale_shim(self) -> None:
        with FakeEnvironment() as fake:
            # Group-writable shim must be ignored in favor of AppImage
            fake.install_orca_shim(
                fake.make_orca_script_body(
                    bots=[{"id": "s", "name": "from-shim", "agent": "codex", "paused": False}],
                    tag="badshim",
                ),
                mode=0o775,
            )
            fake.install_orca_appimage(
                fake.make_orca_script_body(
                    bots=[{"id": "a", "name": "from-app", "agent": "codex", "paused": False}],
                    tag="app",
                ),
                mode=0o755,
            )
            res = run_cli(["--source", "orca", "--format", "json"], env=fake.env())
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertEqual([r["session"] for r in json.loads(res.stdout)], ["from-app"])

        with FakeEnvironment() as fake:
            # Valid permissions shim that reports "Orca CLI is unavailable" retries once with AppImage
            fake.install_orca_shim(
                fake.make_orca_script_body(
                    bot_list_rc=1,
                    bot_list_stderr="Orca CLI is unavailable\n",
                    tag="staleshim",
                ),
                mode=0o755,
            )
            fake.install_orca_appimage(
                fake.make_orca_script_body(
                    bots=[{"id": "fb", "name": "fallback-bot", "agent": "codex", "paused": False}],
                    tag="fbapp",
                ),
                mode=0o755,
            )
            res = run_cli(["--source", "orca", "--format", "json"], env=fake.env())
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertEqual([r["session"] for r in json.loads(res.stdout)], ["fallback-bot"])


if __name__ == "__main__":
    unittest.main()
