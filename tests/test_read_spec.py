"""Independent spec-based black-box unit tests for `agent-sessions read`."""

from __future__ import annotations

import json
import unittest

from tests.helpers import (
    FakeEnvironment,
    ISO_UTC_RE,
    READ_SCHEMA_KEYS,
    run_cli,
    sample_herdr_process_info,
    sample_herdr_snapshot,
)


class ReadCommandSpecTests(unittest.TestCase):
    def _install_common(self, fake: FakeEnvironment) -> None:
        fake.install_herdr_stub(
            snapshot=sample_herdr_snapshot(),
            process_info=sample_herdr_process_info(),
            pane_read={
                "w1:p1": (0, "line1\n\x1b[31mred\x1b[0m\tcol\nline3\n\n\n", ""),
                "w2:p1": (0, "\n\n", ""),
            },
        )
        fake.install_tmux_stub(
            panes=[
                {
                    "session_name": "demo",
                    "window_index": "0",
                    "pane_index": "0",
                    "pane_id": "%0",
                    "pane_title": "sh",
                    "pane_current_command": "cat",
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
                    "session_name": "dead-sess",
                    "window_index": "0",
                    "pane_index": "0",
                    "pane_id": "%9",
                    "pane_title": "exited",
                    "pane_current_command": "sh",
                    "pane_pid": "999991",
                    "pane_dead": "1",
                    "pane_active": "1",
                    "window_active": "1",
                    "session_attached": "0",
                    "pane_current_path": "/tmp/dead",
                    "pid": "10",
                    "start_time": "100",
                },
            ],
            captures={
                "%0": (0, "alpha\n\x1b[32mgreen\x1b[0m\x07\nbeta\n\n", ""),
                "%9": (0, "last words before exit\n\n", ""),
            },
        )
        orca_body = fake.make_orca_script_body(
            bots=[
                {"id": "b1", "name": "helper", "agent": "codex", "paused": False},
                {"id": "b2", "name": "reviewer", "agent": "claude", "paused": False},
                {"id": "b3", "name": "emptybot", "agent": "gemini", "paused": False},
            ],
            chats_by_bot={
                "helper": [{"id": "chat-helper-1", "home": True}],
                "reviewer": [{"id": "chat-rev-1", "home": True}],
                "emptybot": [{"id": "chat-empty-1", "home": True}],
            },
            inbox_messages=[
                # Newest first in inbox (as Orca returns); CLI must return chronological order!
                {
                    "id": "msg_3",
                    "type": "status",
                    "from_handle": "orca_session_id:chat-rev-1",
                    "to_handle": "run:run_helper_1",
                    "subject": "Reply",
                    "body": "All green\x1b[33m!\x1b[0m\tOK",
                    "created_at": "2026-01-01T09:05:00Z",
                    "thread_id": "msg_1",
                },
                {
                    "id": "msg_2",
                    "type": "question",
                    "from_handle": "external:user",
                    "to_handle": "orca_session_id:chat-helper-1",
                    "subject": "Status?",
                    "body": "Any update?",
                    "created_at": "2026-01-01T09:02:00Z",
                    "thread_id": None,
                },
                {
                    "id": "msg_1",
                    "run_id": "run_helper_1",
                    "type": "status",
                    "from_handle": "orca_session_id:chat-helper-1",
                    "to_handle": "dispatch:ctx_1",
                    "subject": "Kickoff",
                    "body": "Start run",
                    "created_at": "2026-01-01T09:00:00Z",
                    "thread_id": None,
                },
            ],
        )
        fake.install_orca_shim(orca_body)

    def test_unified_schema_and_structured_vs_raw_across_all_three_sources(self) -> None:
        with FakeEnvironment() as fake:
            self._install_common(fake)
            r_herdr = run_cli(["read", "--format", "json", "herdr:my-app/1"], env=fake.env())
            r_tmux = run_cli(["read", "--format", "json", "tmux:demo"], env=fake.env())
            r_orca = run_cli(["read", "--format", "json", "orca:helper"], env=fake.env())

            for label, r in (("herdr", r_herdr), ("tmux", r_tmux), ("orca", r_orca)):
                self.assertEqual(r.returncode, 0, f"{label}: {r.stderr}")

            d_herdr = json.loads(r_herdr.stdout)
            d_tmux = json.loads(r_tmux.stdout)
            d_orca = json.loads(r_orca.stdout)

            self.assertEqual(len(d_herdr), 1)
            self.assertEqual(len(d_tmux), 1)
            self.assertEqual(len(d_orca), 3)

            # Exact same 11-field schema on every record across all 3 sources
            for rec in [*d_herdr, *d_tmux, *d_orca]:
                self.assertEqual(set(rec.keys()), READ_SCHEMA_KEYS)
                self.assertRegex(rec["timestamp"], ISO_UTC_RE)

            # Raw terminal sources: structured=False, type="terminal-output", null id/to/subject/thread_id
            for rec, expected_src, expected_target in (
                (d_herdr[0], "herdr", "herdr:my-app/1"),
                (d_tmux[0], "tmux", "tmux:demo:0.0"),
            ):
                self.assertEqual(rec["source"], expected_src)
                self.assertEqual(rec["target"], expected_target)
                self.assertFalse(rec["structured"])
                self.assertEqual(rec["type"], "terminal-output")
                self.assertEqual(rec["from"], expected_target)
                self.assertIsNone(rec["id"])
                self.assertIsNone(rec["to"])
                self.assertIsNone(rec["subject"])
                self.assertIsNone(rec["thread_id"])

            # Orca structured source: structured=True, chronological order, participant mapping
            self.assertEqual([m["id"] for m in d_orca], ["msg_1", "msg_2", "msg_3"])
            for rec in d_orca:
                self.assertEqual(rec["source"], "orca")
                self.assertEqual(rec["target"], "orca:helper")
                self.assertTrue(rec["structured"])
                self.assertNotEqual(rec["type"], "terminal-output")

            self.assertEqual(d_orca[1]["from"], "external:user")
            self.assertEqual(d_orca[1]["to"], "bot:helper")
            self.assertEqual(d_orca[2]["from"], "bot:reviewer")
            self.assertEqual(d_orca[2]["to"], "bot:helper")
            self.assertEqual(d_orca[2]["thread_id"], "msg_1")

    def test_table_strips_ansi_and_controls_keeping_tabs_newlines_json_preserves(self) -> None:
        with FakeEnvironment() as fake:
            self._install_common(fake)
            # Herdr: JSON preserves ANSI + tab, trailing empty lines removed
            r_json = run_cli(["read", "--format", "json", "herdr:w1:p1"], env=fake.env())
            self.assertEqual(r_json.returncode, 0, r_json.stderr)
            body_json = json.loads(r_json.stdout)[0]["body"]
            self.assertIn("\x1b[31mred\x1b[0m\tcol", body_json)
            self.assertFalse(body_json.endswith("\n"))

            # Herdr: table strips ANSI escape sequences, preserves tab and newlines
            r_tbl = run_cli(["read", "--format", "table", "herdr:w1:p1"], env=fake.env())
            self.assertEqual(r_tbl.returncode, 0, r_tbl.stderr)
            self.assertNotIn("\x1b", r_tbl.stdout)
            self.assertIn("red\tcol", r_tbl.stdout)
            self.assertIn("terminal-output", r_tbl.stdout)

            # tmux: JSON preserves ANSI + BEL (\x07); table strips both
            t_json = run_cli(["read", "--format", "json", "tmux:%0"], env=fake.env())
            self.assertIn("\x1b[32mgreen\x1b[0m\x07", json.loads(t_json.stdout)[0]["body"])
            t_tbl = run_cli(["read", "tmux:%0"], env=fake.env())
            self.assertNotIn("\x1b", t_tbl.stdout)
            self.assertNotIn("\x07", t_tbl.stdout)
            self.assertIn("green", t_tbl.stdout)

            # Orca: JSON preserves ANSI; table strips ANSI and shows subject/id/participants
            o_json = run_cli(["read", "--format", "json", "orca:helper"], env=fake.env())
            self.assertIn("\x1b[33m!\x1b[0m\tOK", json.loads(o_json.stdout)[2]["body"])
            o_tbl = run_cli(["read", "orca:helper"], env=fake.env())
            self.assertNotIn("\x1b", o_tbl.stdout)
            self.assertIn("All green!\tOK", o_tbl.stdout)
            self.assertIn("msg_3", o_tbl.stdout)
            self.assertIn("Reply", o_tbl.stdout)

    def test_empty_pane_produces_one_record_and_empty_orca_produces_empty_list(self) -> None:
        with FakeEnvironment() as fake:
            self._install_common(fake)
            r_empty_pane = run_cli(["read", "--format", "json", "herdr:shell-ws/1"], env=fake.env())
            self.assertEqual(r_empty_pane.returncode, 0, r_empty_pane.stderr)
            recs = json.loads(r_empty_pane.stdout)
            self.assertEqual(len(recs), 1)
            self.assertEqual(recs[0]["body"], "")

            r_empty_orca = run_cli(["read", "--format", "json", "orca:emptybot"], env=fake.env())
            self.assertEqual(r_empty_orca.returncode, 0, r_empty_orca.stderr)
            self.assertEqual(json.loads(r_empty_orca.stdout), [])

    def test_read_dead_tmux_pane_succeeds(self) -> None:
        with FakeEnvironment() as fake:
            self._install_common(fake)
            res = run_cli(["read", "--format", "json", "tmux:dead-sess:0.0"], env=fake.env())
            self.assertEqual(res.returncode, 0, res.stderr)
            recs = json.loads(res.stdout)
            self.assertEqual(len(recs), 1)
            self.assertEqual(recs[0]["body"], "last words before exit")

    def test_read_limit_for_terminals_and_orca(self) -> None:
        with FakeEnvironment() as fake:
            self._install_common(fake)
            r_tmux = run_cli(["read", "--format", "json", "--limit", "2", "tmux:demo"], env=fake.env())
            self.assertEqual(r_tmux.returncode, 0, r_tmux.stderr)
            lines = json.loads(r_tmux.stdout)[0]["body"].splitlines()
            self.assertEqual(len(lines), 2)
            self.assertEqual(lines[-1], "beta")

            # Orca --limit 2 returns the 2 most recent messages in chronological order
            r_orca = run_cli(["read", "--format", "json", "--limit", "2", "orca:helper"], env=fake.env())
            self.assertEqual(r_orca.returncode, 0, r_orca.stderr)
            ids = [m["id"] for m in json.loads(r_orca.stdout)]
            self.assertEqual(ids, ["msg_2", "msg_3"])

    def test_unprefixed_target_searches_herdr_and_tmux_not_orca_and_ids_require_prefix(self) -> None:
        with FakeEnvironment() as fake:
            self._install_common(fake)
            # Unprefixed Herdr name works
            self.assertEqual(run_cli(["read", "--format", "json", "my-app/1"], env=fake.env()).returncode, 0)
            # Unprefixed tmux name works
            self.assertEqual(run_cli(["read", "--format", "json", "demo"], env=fake.env()).returncode, 0)
            # Unprefixed Orca bot name must NOT match Orca -> exit 4
            self.assertEqual(run_cli(["read", "helper"], env=fake.env()).returncode, 4)
            # Unprefixed IDs must NOT match -> exit 4
            self.assertEqual(run_cli(["read", "w1:p1"], env=fake.env()).returncode, 4)
            self.assertEqual(run_cli(["read", "%0"], env=fake.env()).returncode, 4)

    def test_read_exit_codes_2_3_4_5_6(self) -> None:
        # Exit 2: invalid arguments / limit out of 1..1000
        with FakeEnvironment() as fake:
            self._install_common(fake)
            for bad in (
                ["read"],
                ["read", "--limit", "0", "tmux:demo"],
                ["read", "--limit", "-1", "tmux:demo"],
                ["read", "--limit", "1001", "tmux:demo"],
                ["read", "--limit", "abc", "tmux:demo"],
                ["read", "--format", "text", "tmux:demo"],
            ):
                with self.subTest(args=bad):
                    res = run_cli(bad, env=fake.env())
                    self.assertEqual(res.returncode, 2, f"args={bad} stderr={res.stderr}")

        # Exit 3: requested source unavailable (missing herdr binary, unreadable Orca inbox)
        with FakeEnvironment() as fake:
            self.assertEqual(run_cli(["read", "herdr:my-app/1"], env=fake.env()).returncode, 3)
            fake.install_orca_shim(
                fake.make_orca_script_body(
                    bots=[{"id": "b1", "name": "helper", "agent": "codex", "paused": False}],
                    inbox_rc=1,
                    inbox_stderr="inbox error\n",
                )
            )
            self.assertEqual(run_cli(["read", "orca:helper"], env=fake.env()).returncode, 3)

        # Exit 4: missing target
        with FakeEnvironment() as fake:
            self._install_common(fake)
            self.assertEqual(run_cli(["read", "herdr:no-such/1"], env=fake.env()).returncode, 4)
            self.assertEqual(run_cli(["read", "tmux:no-such"], env=fake.env()).returncode, 4)
            self.assertEqual(run_cli(["read", "orca:no-such-bot"], env=fake.env()).returncode, 4)

        # Exit 5: ambiguous target (same name in both Herdr and tmux or duplicate panes)
        with FakeEnvironment() as fake:
            snap = sample_herdr_snapshot()
            snap["result"]["snapshot"]["workspaces"][1]["label"] = "my-app"
            fake.install_herdr_stub(snapshot=snap, process_info=sample_herdr_process_info())
            res = run_cli(["read", "herdr:my-app/1"], env=fake.env())
            self.assertEqual(res.returncode, 5, res.stderr)
            self.assertTrue(res.stderr.strip())

        # Exit 6: resolved pane could not be captured
        with FakeEnvironment() as fake:
            fake.install_herdr_stub(
                snapshot=sample_herdr_snapshot(),
                process_info=sample_herdr_process_info(),
                pane_read={"w1:p1": (1, "", "read failure\n")},
            )
            self.assertEqual(run_cli(["read", "herdr:my-app/1"], env=fake.env()).returncode, 6)

    def test_read_missing_tmux_server_exits_4_not_3(self) -> None:
        # Spec: missing tmux server counts as an empty result (so target not found -> 4).
        with FakeEnvironment() as fake:
            fake.install_tmux_stub(
                list_rc=1,
                list_stderr="no server running on /tmp/tmux-1000/default\n",
            )
            res = run_cli(["read", "tmux:demo"], env=fake.env())
            self.assertEqual(res.returncode, 4, res.stderr)

    def test_read_herdr_limit_truncates_after_stripping_trailing_empty_lines(self) -> None:
        with FakeEnvironment() as fake:
            fake.install_herdr_stub(
                snapshot=sample_herdr_snapshot(),
                process_info=sample_herdr_process_info(),
                pane_read={"w1:p1": (0, "L1\nL2\nL3\nL4\nL5\n\n\n", "")},
            )
            res = run_cli(["read", "--format", "json", "--limit", "2", "herdr:my-app/1"], env=fake.env())
            self.assertEqual(res.returncode, 0, res.stderr)
            body = json.loads(res.stdout)[0]["body"]
            self.assertEqual(body.splitlines(), ["L4", "L5"])

    def test_read_ambiguous_target_prints_disambiguating_pane_ids(self) -> None:
        with FakeEnvironment() as fake:
            snap = sample_herdr_snapshot()
            snap["result"]["snapshot"]["workspaces"][1]["label"] = "my-app"
            fake.install_herdr_stub(snapshot=snap, process_info=sample_herdr_process_info())
            res = run_cli(["read", "herdr:my-app/1"], env=fake.env())
            self.assertEqual(res.returncode, 5)
            self.assertIn("w1:p1", res.stderr)
            self.assertIn("w2:p1", res.stderr)


if __name__ == "__main__":
    unittest.main()
