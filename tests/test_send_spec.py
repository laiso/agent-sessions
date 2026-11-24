"""Independent spec-based black-box unit tests for `agent-sessions send`."""

from __future__ import annotations

import json
import unittest

from tests.helpers import (
    FakeEnvironment,
    run_cli,
    sample_herdr_process_info,
    sample_herdr_snapshot,
)


class SendCommandSpecTests(unittest.TestCase):
    def _install_common(self, fake: FakeEnvironment, *, herdr_send_rc: int = 0, tmux_send_rc: int = 0) -> None:
        fake.install_herdr_stub(
            snapshot=sample_herdr_snapshot(),
            process_info=sample_herdr_process_info(),
            pane_send_rc=herdr_send_rc,
            pane_send_stderr="herdr send failed\n" if herdr_send_rc else "",
        )
        fake.install_tmux_stub(
            panes=[
                {
                    "session_name": "demo",
                    "window_index": "0",
                    "pane_index": "0",
                    "pane_id": "%0",
                    "pane_title": "cat",
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
                    "pane_title": "dead",
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
            send_rc=tmux_send_rc,
            send_stderr="tmux send failed\n" if tmux_send_rc else "",
        )
        fake.install_orca_shim(
            fake.make_orca_script_body(
                bots=[{"id": "b1", "name": "helper", "agent": "codex", "paused": False}]
            )
        )

    def test_dry_run_text_and_json_with_and_without_enter(self) -> None:
        with FakeEnvironment() as fake:
            self._install_common(fake)
            r_txt = run_cli(
                ["send", "--dry-run", "herdr:my-app/1", "How", "is", "the", "build?"],
                env=fake.env(),
            )
            self.assertEqual(r_txt.returncode, 0, r_txt.stderr)
            self.assertIn("Would send to herdr my-app/1 (w1:p1)", r_txt.stdout)
            self.assertIn("> How is the build? ⏎", r_txt.stdout)

            r_no_enter = run_cli(
                ["send", "--dry-run", "--no-enter", "herdr:w1:p1", "--", "--help"],
                env=fake.env(),
            )
            self.assertEqual(r_no_enter.returncode, 0, r_no_enter.stderr)
            self.assertIn("> --help", r_no_enter.stdout)
            self.assertNotIn("⏎", r_no_enter.stdout)

            r_json = run_cli(
                ["send", "--dry-run", "--format", "json", "tmux:%0", "hello"],
                env=fake.env(),
            )
            self.assertEqual(r_json.returncode, 0, r_json.stderr)
            data = json.loads(r_json.stdout)
            self.assertFalse(data["sent"])
            self.assertTrue(data["dry_run"])
            self.assertTrue(data["enter"])
            self.assertEqual(data["message"], "hello")
            self.assertEqual(data["target"]["pane_id"], "%0")

            # Verify no send-keys / pane send was called during dry-run
            for call in fake.tmux_calls():
                self.assertNotIn("send-keys", call)

    def test_actual_send_to_tmux_joins_message_args(self) -> None:
        with FakeEnvironment() as fake:
            self._install_common(fake)
            res = run_cli(
                ["send", "--format", "json", "tmux:demo", "echo", "hello"],
                env=fake.env(),
            )
            self.assertEqual(res.returncode, 0, res.stderr)
            data = json.loads(res.stdout)
            self.assertTrue(data["sent"])
            self.assertFalse(data["dry_run"])
            self.assertTrue(data["enter"])
            self.assertEqual(data["message"], "echo hello")

    def test_message_validation_boundaries_and_control_chars(self) -> None:
        with FakeEnvironment() as fake:
            self._install_common(fake)
            # Exactly 4096 chars must succeed
            msg_4096 = "a" * 4096
            ok = run_cli(["send", "--dry-run", "tmux:demo", msg_4096], env=fake.env())
            self.assertEqual(ok.returncode, 0, ok.stderr)

            # 4097 chars must fail with exit 2
            msg_4097 = "a" * 4097
            too_long = run_cli(["send", "--dry-run", "tmux:demo", msg_4097], env=fake.env())
            self.assertEqual(too_long.returncode, 2)

            # Empty, whitespace-only, tab, newline, CR, ESC, BEL, DEL -> exit 2
            for bad_msg in ("", "   ", "a\tb", "a\nb", "a\rb", "a\x1bb", "a\x07b", "a\x7fb"):
                with self.subTest(msg=repr(bad_msg)):
                    res = run_cli(["send", "--dry-run", "tmux:demo", bad_msg], env=fake.env())
                    self.assertEqual(res.returncode, 2, f"msg={bad_msg!r} rc={res.returncode}")

    def test_send_dead_pane_missing_target_and_orca_target(self) -> None:
        with FakeEnvironment() as fake:
            self._install_common(fake)
            # Dead tmux pane -> exit 4
            self.assertEqual(
                run_cli(["send", "--dry-run", "tmux:dead-sess:0.0", "hi"], env=fake.env()).returncode,
                4,
            )
            # Missing target -> exit 4
            self.assertEqual(
                run_cli(["send", "--dry-run", "tmux:nope", "hi"], env=fake.env()).returncode,
                4,
            )
            # Unprefixed IDs -> exit 4
            self.assertEqual(
                run_cli(["send", "--dry-run", "%0", "hi"], env=fake.env()).returncode,
                4,
            )
            self.assertEqual(
                run_cli(["send", "--dry-run", "w1:p1", "hi"], env=fake.env()).returncode,
                4,
            )
            # Orca bot is NOT a valid send target (only for read) -> non-zero (expect 2)
            r_orca = run_cli(["send", "--dry-run", "orca:helper", "hi"], env=fake.env())
            self.assertEqual(r_orca.returncode, 2, f"stderr={r_orca.stderr}")

    def test_send_exit_3_5_6(self) -> None:
        # Exit 3: all requested sources failed
        with FakeEnvironment() as fake:
            res = run_cli(["send", "herdr:my-app/1", "hi"], env=fake.env())
            self.assertEqual(res.returncode, 3)

        # Exit 5: ambiguous target + candidates printed to stderr
        with FakeEnvironment() as fake:
            snap = sample_herdr_snapshot()
            snap["result"]["snapshot"]["workspaces"][1]["label"] = "my-app"
            fake.install_herdr_stub(snapshot=snap, process_info=sample_herdr_process_info())
            res = run_cli(["send", "--dry-run", "herdr:my-app/1", "hi"], env=fake.env())
            self.assertEqual(res.returncode, 5)
            self.assertIn("w1:p1", res.stderr)
            self.assertIn("w2:p1", res.stderr)

        # Exit 6: delivery failed
        with FakeEnvironment() as fake:
            self._install_common(fake, tmux_send_rc=1)
            res = run_cli(["send", "tmux:demo", "hi"], env=fake.env())
            self.assertEqual(res.returncode, 6)

    def test_send_invalid_format_table_exits_2(self) -> None:
        with FakeEnvironment() as fake:
            self._install_common(fake)
            res = run_cli(["send", "--dry-run", "--format", "table", "tmux:demo", "hi"], env=fake.env())
            self.assertEqual(res.returncode, 2)


if __name__ == "__main__":
    unittest.main()
