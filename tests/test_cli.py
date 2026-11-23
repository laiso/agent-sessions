import contextlib
import io
import json
import unittest

from support import fixture, response
from agent_sessions.cli import main


class CliTests(unittest.TestCase):
    def invoke(self, args, runner):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(args, runner)
        return code, out.getvalue(), err.getvalue()

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
