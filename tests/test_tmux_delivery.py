"""Delivery checks against disposable, detached tmux servers."""
import contextlib
import io
import os
import shutil
import subprocess
import tempfile
import time
import unittest

import support
from agent_sessions.cli import main


@unittest.skipUnless(shutil.which("tmux"), "tmux is not installed")
class TmuxDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="agent-sessions-test-")
        self.addCleanup(self.tmp.cleanup)
        self.env = dict(os.environ, TMUX_TMPDIR=self.tmp.name)
        self.env.pop("TMUX", None)
        self.env.pop("TMUX_PANE", None)
        self.start_server()
        self.addCleanup(self.tmux, "kill-server")

    def run_command(self, argv):
        return subprocess.run(argv, env=self.env, capture_output=True, text=True, timeout=5)

    def tmux(self, *args):
        result = self.run_command(["tmux", *args])
        result.check_returncode()
        return result.stdout.strip()

    def wait_for_text(self, marker):
        deadline = time.monotonic() + 2
        while marker not in self.tmux("capture-pane", "-p", "-t", "%0"):
            if time.monotonic() >= deadline:
                self.fail(f"target pane did not show {marker!r}")
            time.sleep(0.01)

    def start_server(self):
        self.tmux("-f", "/dev/null", "new", "-d", "-s", "review", "cat")
        self.tmux("set-buffer", "-b", "keep", "user data")

    def invoke(self, runner, *opts):
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = main(["send", *opts, "tmux:%0", "--", "-日本語\\;"], runner)
        self.assertEqual(self.tmux("list-buffers", "-F", "#{buffer_name}"), "keep")
        self.assertEqual(self.tmux("show-buffer", "-b", "keep"), "user data")
        return code, err.getvalue()

    def test_synchronized_neighbor_gets_neither_text_nor_enter(self):
        neighbor = self.tmux("split-window", "-P", "-F", "#{pane_id}", "-t", "%0", "cat")
        self.tmux("set-window-option", "-t", "review", "synchronize-panes", "on")
        self.assertEqual(self.invoke(self.run_command, "--no-enter"), (0, ""))
        self.assertEqual(self.invoke(self.run_command), (0, ""))
        self.wait_for_text("-日本語\\;-日本語\\;")
        self.assertEqual(self.tmux("display-message", "-p", "-t", neighbor, "#{cursor_y}:#{cursor_x}"), "0:0")

    def test_reused_pane_id_in_replacement_server(self):
        for phase in ("text", "enter"):
            with self.subTest(phase=phase):
                def replace_server(argv):
                    if argv[1] == "set-buffer" and (argv[5] == "\n") == (phase == "enter"):
                        self.tmux("kill-server")
                        # A killed server may still own its socket briefly.
                        # A fresh endpoint gives the next client the replacement
                        # server with the same %0 without racing socket teardown.
                        self.env["TMUX_TMPDIR"] = tempfile.mkdtemp(dir=self.tmp.name)
                        self.start_server()
                    return self.run_command(argv)

                code, reason = self.invoke(replace_server)
                self.assertEqual(code, 6)
                self.assertIn("target pane changed", reason)
                if phase == "enter":
                    self.assertIn("text sent", reason)
                self.assertEqual(self.tmux("display-message", "-p", "-t", "%0", "#{cursor_y}:#{cursor_x}"), "0:0")

    def test_input_disabled_is_not_reported_as_sent(self):
        self.tmux("select-pane", "-d", "-t", "%0")
        code, reason = self.invoke(self.run_command)
        self.assertEqual(code, 6)
        self.assertIn("input disabled", reason)

    def test_lost_acknowledgement_after_paste(self):
        def lose_reply(argv):
            result = self.run_command(argv)
            if argv[1] == "set-buffer":
                result.check_returncode()
                raise subprocess.TimeoutExpired(argv, 5)
            return result

        code, reason = self.invoke(lose_reply)
        self.assertEqual(code, 6)
        self.assertIn("text delivery not confirmed", reason)
        self.wait_for_text("-日本語\\;")

    def test_hook_output_cannot_hide_refusal_and_trigger_enter(self):
        self.tmux("set-hook", "-g", "after-set-buffer", "display-message -p notice")
        self.tmux("select-pane", "-d", "-t", "%0")
        submitted = []

        def restore_input_after_text(argv):
            result = self.run_command(argv)
            if argv[1] == "set-buffer":
                submitted.append(argv[5])
                self.tmux("select-pane", "-e", "-t", "%0")
            return result

        code, reason = self.invoke(restore_input_after_text)
        self.assertEqual(code, 6)
        self.assertIn("nothing sent", reason)
        self.assertEqual(submitted, [r"-日本語\\;"])
        self.assertEqual(self.tmux("display-message", "-p", "-t", "%0", "#{cursor_y}:#{cursor_x}"), "0:0")

    def test_hook_output_cannot_report_false_refusal(self):
        self.tmux("set-hook", "-g", "after-set-buffer", "display-message -p pane-unavailable")
        self.assertEqual(self.invoke(self.run_command), (0, ""))
        self.wait_for_text("-日本語\\;")
