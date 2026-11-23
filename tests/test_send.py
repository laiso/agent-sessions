import contextlib
import io
import json
import unittest

from support import fixture, response
from agent_sessions.cli import main
from agent_sessions.send import message_text, tmux_command


class SendTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.read_fail = set()
        self.fail_arg = None
        self.err = io.StringIO()
        self.out = io.StringIO()

    def runner(self, argv):
        if argv[0] in self.read_fail:
            return response(stderr="connection failed", code=1)
        if argv == ["herdr", "api", "snapshot"]:
            return response(json.dumps(fixture("herdr_snapshot")))
        if argv[:3] == ["herdr", "pane", "process-info"]:
            return response("{}")
        if argv[:2] == ["tmux", "list-panes"]:
            fields = ["dev", "0", "3", "%8", "work", "bash", "invalid", "0", "1", "1", "0", "/tmp"]
            return response("\x1f".join(fields))
        self.calls.append(argv)
        if argv[-1] == self.fail_arg:
            return response(stdout='{"error":{"message":"pane unavailable"}}', code=1)
        return response()

    def invoke(self, *args):
        with contextlib.redirect_stdout(self.out), contextlib.redirect_stderr(self.err):
            return main(["send", *args], self.runner)

    def test_herdr_text_and_enter(self):
        self.assertEqual(self.invoke("herdr:wsh-2027-pre/1", "hello", "world"), 0)
        self.assertEqual(self.calls, [["herdr", "pane", "send-text", "wN:pH", "hello world"],
                                      ["herdr", "pane", "send-keys", "wN:pH", "enter"]])
        self.assertEqual(self.out.getvalue(), "Sent to herdr wsh-2027-pre/1 (wN:pH) [agy, idle]\n> hello world ⏎\n")

    def test_herdr_dash_messages(self):
        for text in ("--help", "-hello", "--", "--session=test", "--remote", "--remote-keybindings=local"):
            self.calls.clear()
            self.assertEqual(self.invoke("--no-enter", "herdr:wN:pH", "--", text), 0)
            self.assertTrue(all(cmd[:-1] == ["herdr", "pane", "send-text", "wN:pH"] for cmd in self.calls))
            self.assertEqual("".join(cmd[-1] for cmd in self.calls), text)
            if text.startswith(("--session", "--remote")):
                self.assertEqual(self.calls[0][-1], "-")

    def test_message_validation_precedes_collection(self):
        def unexpected(argv):
            self.fail(f"unexpected command {argv}")
        for msg in ("", " \t", "x\n", "x\r", "x\0", "x\x1b", "x\x7f", "x\x85", "x\u2028", "あ" * 4097):
            with self.subTest(msg=msg[:10]), self.assertRaises(SystemExit) as error, contextlib.redirect_stderr(self.err):
                main(["send", "dev", msg], unexpected)
            self.assertEqual(error.exception.code, 2)
        self.assertEqual(message_text(["あ" * 4096]), "あ" * 4096)
        self.assertEqual(message_text(["a b", " c "]), "a b  c ")
        with self.assertRaisesRegex(ValueError, "tab"):
            message_text(["a\tb"])

    def test_tmux_detached_no_enter_and_json(self):
        text = "Enter; echo $HOME"
        self.assertEqual(self.invoke("--no-enter", "--format", "json", "tmux:dev", text), 0)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0], tmux_command("%8", text, self.calls[0][3]))
        target = dict(source="tmux", session="dev", pane="0.3 work", pane_id="%8", agent=None,
                      status="shell", command="bash", cwd="/tmp", focused=False)
        self.assertEqual(json.loads(self.out.getvalue()), dict(sent=True, dry_run=False, enter=False,
                                                               target=target, message=text))

    def test_tmux_enter(self):
        self.assertEqual(self.invoke("tmux:%8", "--", "-abc"), 0)
        self.assertEqual([c[5] for c in self.calls], ["-abc", "\n"])
        self.assertTrue(all("send-keys" not in c for c in self.calls))
        self.assertIn("dev:0.3 (%8) [shell]", self.out.getvalue())

    def test_dry_run(self):
        self.assertEqual(self.invoke("--dry-run", "--format", "json", "tmux:dev", "hi"), 0)
        result = json.loads(self.out.getvalue())
        self.assertEqual((result["sent"], result["dry_run"], result["enter"]), (False, True, True))
        self.assertEqual(self.calls, [])

    def test_read_failure_and_missing(self):
        self.read_fail.add("herdr")
        self.assertEqual(self.invoke("--dry-run", "dev", "hi"), 0)
        self.assertIn("agent-sessions: herdr: connection failed", self.err.getvalue())
        self.assertEqual(self.invoke("herdr:missing", "hi"), 3)
        self.assertEqual(self.invoke("tmux:missing", "hi"), 4)
        self.assertEqual(self.calls, [])

    def test_text_failure_skips_enter(self):
        self.fail_arg = "hi"
        self.assertEqual(self.invoke("herdr:wN:pH", "hi"), 6)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.out.getvalue(), "")
        self.assertEqual(self.err.getvalue(), "agent-sessions: send: pane unavailable\n")

    def test_partial_text_failure(self):
        self.fail_arg = "-session=test"
        self.assertEqual(self.invoke("herdr:wN:pH", "--", "--session=test"), 6)
        self.assertEqual(len(self.calls), 2)
        self.assertIn("leading '-' sent", self.err.getvalue())

    def test_enter_failure(self):
        self.fail_arg = "enter"
        self.assertEqual(self.invoke("herdr:wN:pH", "hi"), 6)
        self.assertEqual(len(self.calls), 2)
        self.assertIn("text sent, Enter failed: pane unavailable", self.err.getvalue())


class TmuxSafetyTests(unittest.TestCase):
    def target(self):
        from agent_sessions.model import TmuxSession
        return TmuxSession("tmux", "dev", "1.0", "%3", None, "shell", "bash", "/tmp", False, True)

    def test_paste_only_unique_buffers(self):
        """paste-buffer ignores synchronize-panes; buffer names never collide with the user's."""
        from agent_sessions.send import transmit
        calls = []
        transmit(self.target(), "rm -rf x", True, lambda argv: calls.append(argv) or response())
        self.assertEqual(len(calls), 2)
        for argv, data in zip(calls, ["rm -rf x", "\n"]):
            self.assertNotIn("send-keys", argv)
            self.assertEqual(argv[1:6], ["set-buffer", "-b", argv[3], "--", data])
            self.assertEqual(argv[6:], [";", "paste-buffer", "-d", "-b", argv[3], "-t", "%3"])
        self.assertRegex(calls[0][3], r"^agent-sessions-[0-9a-f]{32}$")
        self.assertNotEqual(calls[0][3], calls[1][3])

    def test_text_failure_reports_nothing_sent_and_cleans(self):
        from agent_sessions.model import SourceError
        from agent_sessions.send import transmit
        calls = []

        def runner(argv):
            calls.append(argv)
            return response(stderr="can't find pane: %3", code=1) if argv[1] == "set-buffer" else response()
        with self.assertRaisesRegex(SourceError, "^nothing sent"):
            transmit(self.target(), "hi", True, runner)
        self.assertEqual(calls[-1], ["tmux", "delete-buffer", "-b", calls[0][3]])

    def test_enter_failure_reports_text_sent(self):
        from agent_sessions.model import SourceError
        from agent_sessions.send import transmit
        calls = []

        def runner(argv):
            calls.append(argv)
            bad = argv[1] == "set-buffer" and argv[5] == "\n"
            return response(stderr="can't find pane", code=1) if bad else response()
        with self.assertRaisesRegex(SourceError, "^text sent, Enter failed"):
            transmit(self.target(), "hi", True, runner)

    def test_cleanup_failure_is_reported(self):
        from agent_sessions.model import SourceError
        from agent_sessions.send import transmit

        def runner(argv):
            if argv[1] == "set-buffer":
                return response(stderr="paste failed", code=1)
            return response(stderr="server exited", code=1)
        with self.assertRaisesRegex(SourceError, "temporary buffer agent-sessions-[0-9a-f]+ may remain"):
            transmit(self.target(), "hi", False, runner)

    def test_cleanup_of_missing_buffer_is_quiet(self):
        from agent_sessions.model import SourceError
        from agent_sessions.send import transmit

        def runner(argv):
            if argv[1] == "set-buffer":
                return response(stderr="paste failed", code=1)
            return response(stderr="no buffer agent-sessions-x", code=1)
        with self.assertRaises(SourceError) as ctx:
            transmit(self.target(), "hi", False, runner)
        self.assertNotIn("may remain", str(ctx.exception))

    def test_trailing_semicolon_escaped(self):
        from agent_sessions.send import tmux_literal
        self.assertEqual(tmux_literal("a;"), "a\\;")
        self.assertEqual(tmux_literal(";"), "\\;")
        self.assertEqual(tmux_literal("b\\;"), "b\\\\;")
        self.assertEqual(tmux_literal("a ; b"), "a ; b")
        self.assertEqual(tmux_command("%3", "x;", "b")[5], "x\\;")
