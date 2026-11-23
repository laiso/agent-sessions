import contextlib
import io
import json
import subprocess
import unittest

from support import fixture, response
from agent_sessions.cli import main
from agent_sessions.send import message_text, tmux_command


def paste_reply(argv):
    return response(f"{argv[3]}-pasted\n")


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
            fields = ["dev", "0", "3", "%8", "work", "bash", "invalid", "0", "1", "1", "0", "/tmp", "321:123456:123"]
            return response("\x1f".join(fields))
        self.calls.append(argv)
        if argv[-1] == self.fail_arg:
            return response(stdout='{"error":{"message":"pane unavailable"}}', code=1)
        return paste_reply(argv) if argv[1] == "set-buffer" else response()

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
        self.assertEqual(self.calls[0][4:6], ["--", text])
        target = dict(source="tmux", session="dev", pane="0.3 work", pane_id="%8", agent=None,
                      status="shell", command="bash", cwd="/tmp", focused=False)
        self.assertEqual(json.loads(self.out.getvalue()), dict(sent=True, dry_run=False, enter=False,
                                                               target=target, message=text))

    def test_enter_timeout_and_cleanup_diagnostics_in_cli(self):
        def runner(argv):
            if argv[1] == "delete-buffer":
                return response(stderr="cleanup error\ndetail", code=1)
            if argv[1] == "set-buffer" and argv[5] == "\n":
                raise subprocess.TimeoutExpired(argv, 5)
            return self.runner(argv)

        with contextlib.redirect_stdout(self.out), contextlib.redirect_stderr(self.err):
            code = main(["send", "tmux:dev", "hi"], runner)
        self.assertEqual(code, 6)
        self.assertEqual(self.out.getvalue(), "")
        lines = self.err.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertIn("text sent, Enter delivery not confirmed", lines[0])
        self.assertIn("may remain (cleanup error detail)", lines[0])
        self.assertIn("timed out after 5 seconds", lines[0])
        self.assertNotIn("321:123456:123", lines[0])

    def test_list_and_send_keep_delivery_identity_private(self):
        with contextlib.redirect_stdout(self.out):
            self.assertEqual(main(["list", "--source", "tmux", "--format", "json"], self.runner), 0)
        listed, = json.loads(self.out.getvalue())
        self.out = io.StringIO()
        self.assertEqual(self.invoke("--dry-run", "--format", "json", "tmux:dev", "hi"), 0)
        self.assertNotIn("delivery_identity", listed)
        self.assertEqual(json.loads(self.out.getvalue())["target"], listed)

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
        return TmuxSession("tmux", "dev", "1.0", "%3", None, "shell", "bash", "/tmp", False, True, "321:123456:123")

    def test_paste_only_unique_buffers(self):
        """paste-buffer ignores synchronize-panes; buffer names never collide with the user's."""
        from agent_sessions.send import transmit
        calls = []
        transmit(self.target(), "rm -rf x", True, lambda argv: calls.append(argv) or paste_reply(argv))
        self.assertEqual(len(calls), 2)
        for argv, data in zip(calls, ["rm -rf x", "\n"]):
            self.assertNotIn("send-keys", argv)
            self.assertEqual(argv[1:6], ["set-buffer", "-b", argv[3], "--", data])
            self.assertEqual(argv[6:11], [";", "if-shell", "-F", "-t", "%3"])
            self.assertEqual(argv[12], f"paste-buffer -d -b {argv[3]} -t %3 ; display-message -p {argv[3]}-pasted")
        self.assertRegex(calls[0][3], r"^agent-sessions-[0-9a-f]{32}$")
        self.assertNotEqual(calls[0][3], calls[1][3])

    def test_text_failure_reports_stage_and_cleans(self):
        from agent_sessions.model import SourceError
        from agent_sessions.send import transmit
        calls = []

        def runner(argv):
            calls.append(argv)
            return response(stderr="can't find pane: %3", code=1) if argv[1] == "set-buffer" else response()
        with self.assertRaisesRegex(SourceError, "^text delivery not confirmed"):
            transmit(self.target(), "hi", True, runner)
        self.assertEqual(calls[-1], ["tmux", "delete-buffer", "-b", calls[0][3]])

    def test_enter_failure_reports_text_sent(self):
        from agent_sessions.model import SourceError
        from agent_sessions.send import transmit
        calls = []

        def runner(argv):
            calls.append(argv)
            bad = argv[1] == "set-buffer" and argv[5] == "\n"
            if bad:
                return response(stderr="can't find pane", code=1)
            return paste_reply(argv) if argv[1] == "set-buffer" else response()
        with self.assertRaisesRegex(SourceError, "^text sent, Enter delivery not confirmed"):
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
            return response(stderr=f"unknown buffer: {argv[3]}", code=1)
        with self.assertRaises(SourceError) as ctx:
            transmit(self.target(), "hi", False, runner)
        self.assertNotIn("may remain", str(ctx.exception))

    def test_cleanup_error_containing_no_buffer_is_reported(self):
        from agent_sessions.model import SourceError
        from agent_sessions.send import transmit

        def runner(argv):
            detail = "paste failed" if argv[1] == "set-buffer" else "no buffer space available"
            return response(stderr=detail, code=1)

        with self.assertRaisesRegex(SourceError, "may remain .*no buffer space available"):
            transmit(self.target(), "hi", False, runner)

    def test_trailing_semicolon_escaped(self):
        from agent_sessions.send import tmux_literal
        self.assertEqual(tmux_literal("a;"), "a\\;")
        self.assertEqual(tmux_literal(";"), "\\;")
        self.assertEqual(tmux_literal("b\\;"), "b\\\\;")
        self.assertEqual(tmux_literal("a ; b"), "a ; b")
        self.assertEqual(tmux_command(self.target(), "x;", "b")[5], "x\\;")


class DeliveryOutcomeTests(unittest.TestCase):
    """A pane rejected by tmux's own check means nothing was pasted: say so plainly."""

    def target(self):
        from agent_sessions.model import TmuxSession
        return TmuxSession("tmux", "dev", "1.0", "%3", None, "shell", "bash", "/tmp", False, True, "1:2:3")

    def test_rejected_text_is_nothing_sent(self):
        from agent_sessions.model import SourceError
        from agent_sessions.send import transmit
        runner = lambda argv: response(f"{argv[3]}-refused\n") if argv[1] == "set-buffer" else response()
        with self.assertRaisesRegex(SourceError, "^nothing sent: target pane changed"):
            transmit(self.target(), "hi", True, runner)

    def test_rejected_enter_is_enter_not_sent(self):
        from agent_sessions.model import SourceError
        from agent_sessions.send import transmit
        calls = []

        def runner(argv):
            calls.append(argv)
            if argv[1] == "set-buffer" and argv[5] == "\n":
                return response(f"{argv[3]}-refused\n")
            return paste_reply(argv) if argv[1] == "set-buffer" else response()
        with self.assertRaisesRegex(SourceError, "^text sent, Enter not sent"):
            transmit(self.target(), "hi", True, runner)

    def test_cleanup_failure_preserves_rejection_of_either_stage(self):
        from agent_sessions.model import SourceError
        from agent_sessions.send import transmit
        for reject, prefix in (("hi", "nothing sent"), ("\n", "text sent, Enter not sent")):
            def runner(argv):
                if argv[1] == "delete-buffer":
                    return response(stderr="cleanup failed\nmore detail", code=1)
                if argv[5] == reject:
                    return response(f"notice\n{argv[3]}-refused\n")
                return paste_reply(argv)

            with self.subTest(reject=reject), self.assertRaises(SourceError) as error:
                transmit(self.target(), "hi", True, runner)
            self.assertTrue(str(error.exception).startswith(prefix))
            self.assertRegex(str(error.exception), r"temporary buffer agent-sessions-[0-9a-f]{32} may remain")
            self.assertIn("cleanup failed\nmore detail", str(error.exception))

    def test_unconfirmed_replies_never_proceed_to_enter(self):
        from agent_sessions.model import SourceError
        from agent_sessions.send import transmit
        for code, reply in ((0, ""), (0, "pane-unavailable"), (0, "{buf}-refused\n{buf}-pasted"),
                            (1, "{buf}-refused")):
            calls = []

            def runner(argv):
                calls.append(argv)
                if argv[1] == "set-buffer":
                    return response(reply.format(buf=argv[3]), "first line\nsecond line", code)
                return response()

            with self.subTest(reply=reply, code=code), self.assertRaisesRegex(SourceError, "^text delivery not confirmed") as error:
                transmit(self.target(), "hi", True, runner)
            self.assertEqual([a[1] for a in calls], ["set-buffer", "delete-buffer"])
            if code:
                self.assertIn("first line\nsecond line", str(error.exception))
