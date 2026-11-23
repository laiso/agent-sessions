import contextlib
import io
import json
import os
import unittest
from unittest.mock import patch

from support import fixture, response
from agent_sessions.cli import main


class CliTests(unittest.TestCase):
    def invoke(self, args, runner):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(args, runner)
        return code, out.getvalue(), err.getvalue()

    def test_list_alias(self):
        args = ["--source", "tmux", "--format", "json"]
        runner = lambda argv: response("")
        self.assertEqual(self.invoke(args, runner), self.invoke(["list", *args], runner))

    def test_partial_success(self):
        def runner(argv):
            if argv[0] == "herdr":
                return response(stderr="error\ndetails", code=1)
            return response(stderr="no server running", code=1)
        self.assertEqual(self.invoke([], runner),
                         (0, "No sessions found.\n", "agent-sessions: herdr: error details\n"))

    def test_all_fail(self):
        def missing(argv):
            raise FileNotFoundError(argv[0])
        code, out, err = self.invoke(["--format", "json"], missing)
        self.assertEqual((code, json.loads(out)), (3, []))
        self.assertEqual(err.splitlines(), ["agent-sessions: herdr: herdr", "agent-sessions: tmux: tmux"])

    def test_source_and_agents_only(self):
        def runner(argv):
            self.assertEqual(argv[0], "herdr")
            return response(json.dumps(fixture("herdr_snapshot")) if argv[1] == "api" else "{}")
        code, out, err = self.invoke(["--source", "herdr", "--agents-only", "--no-header"], runner)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(len(out.splitlines()), 2)
        self.assertTrue(all(line.startswith("herdr") and "agy" in line for line in out.splitlines()))

    @patch.dict(os.environ, {"AGENT_SESSIONS_ORCA": "/test/orca"})
    def test_list_three_sources_and_agents_only(self):
        calls = []

        def runner(argv):
            calls.append(argv[0])
            if argv[0] == "herdr":
                data = fixture("herdr_snapshot") if argv[1] == "api" else {}
            elif argv[0] == "tmux":
                return response("\x1f".join(["dev", "0", "1", "%1", "", "codex", "invalid", "0",
                                             "1", "1", "0", "/tmp", "1:2:3"]))
            elif argv == ["/test/orca", "bot", "list", "--json"]:
                data = fixture("orca_bot_list")
                del data["result"]["bots"][0]["agent"]
            else:
                self.assertEqual(argv, ["/test/orca", "worktree", "ps", "--json"])
                data = fixture("orca_worktree_ps")
            return response(json.dumps(data))

        code, out, err = self.invoke(["--source", "all", "--format", "json", "--agents-only"], runner)
        self.assertEqual((code, err), (0, ""))
        rows = json.loads(out)
        self.assertEqual(list(dict.fromkeys(r["source"] for r in rows)), ["herdr", "tmux", "orca"])
        self.assertEqual([r["session"] for r in rows if r["source"] == "orca"], ["claude", "codex", "gemini"])
        self.assertTrue(all(r["specialty"] is None for r in rows if r["source"] != "orca"))
        self.assertEqual(list(dict.fromkeys(calls)), ["herdr", "tmux", "/test/orca"])

    @patch.dict(os.environ, {"AGENT_SESSIONS_ORCA": "/test/orca"})
    def test_orca_empty_vs_unavailable(self):
        for reply, all_code in ((response('{"ok":true,"result":{"bots":[]}}'), 0),
                                (response("bad"), 3), (response(code=1), 3)):
            def runner(argv):
                return reply if argv[0] == "/test/orca" else response(stderr="unavailable", code=1)
            with self.subTest(reply=reply):
                self.assertEqual(self.invoke(["--source", "orca", "--format", "json"], runner), (0, "[]\n", ""))
                code, out, err = self.invoke(["--format", "json"], runner)
                self.assertEqual((code, out), (all_code, "[]\n"))
                self.assertNotIn("orca", err)

    def test_orca_send_rejected_before_collection(self):
        result = self.invoke(["send", "orca:claude/home", "hello"], lambda argv: self.fail(str(argv)))
        self.assertEqual(result, (4, "", "agent-sessions: send: target not found: orca:claude/home\n"))
        def empty(argv):
            self.assertIn(argv[0], ("herdr", "tmux"))
            return response(code=1)
        self.assertEqual(self.invoke(["send", "claude/home", "hello"], empty)[0], 3)

    def test_invalid_arguments(self):
        with self.assertRaises(SystemExit) as caught:
            self.invoke(["--source", "unknown"], None)
        self.assertEqual(caught.exception.code, 2)


class ErrorMessageTests(unittest.TestCase):
    def test_json_error_message(self):
        from agent_sessions.runner import error_message
        raw = '{"id":"x","error":{"code":"server_not_running","message":"no herdr server is running"}}'
        self.assertEqual(error_message(raw), "no herdr server is running")
        self.assertEqual(error_message("plain failure\n"), "plain failure")
        self.assertEqual(error_message('{"error": 1}'), '{"error": 1}')

    def test_structured_message_preferred_over_plain_stderr(self):
        from agent_sessions.model import SourceError
        from agent_sessions.runner import output
        raw = '{"error":{"message":"no herdr server is running"}}'
        with self.assertRaises(SourceError) as ctx:
            output(lambda argv: response(stdout=raw, stderr="config warning", code=1), ["herdr"])
        self.assertEqual(str(ctx.exception), "no herdr server is running")
        with self.assertRaises(SourceError) as ctx:
            output(lambda argv: response(stdout="", stderr="plain", code=1), ["x"])
        self.assertEqual(str(ctx.exception), "plain")
