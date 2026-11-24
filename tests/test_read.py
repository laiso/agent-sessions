import contextlib
import io
import json
import os
import unittest
from unittest.mock import patch

from support import response
from agent_sessions.cli import main


FIELDS = set("source target id type structured from to subject body timestamp thread_id".split())


def mail(n, sender, recipient, run="work"):
    return dict(id=f"msg_{n}", from_handle=sender, to_handle=recipient, run_id=run,
                type="status", subject="Update", body=f"line {n}\n次", thread_id=None,
                created_at=f"2026-01-01T00:00:0{n}Z")


class ReadTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, AGENT_SESSIONS_ORCA="test-orca")
        env.start()
        self.addCleanup(env.stop)
        self.calls = []
        self.fail_at = None
        self.capture = "old\n\n日本\x1b[31m語\x1b[0m\n\n"
        a, b = "orca_session_id:alpha", "orca_session_id:beta"
        self.inbox = [mail(6, "run:other", "dispatch:new"), mail(5, "term_one", "run:work"),
                      mail(4, "run:other", a), mail(3, b, "dispatch:test", "other"),
                      mail(2, a, "dispatch:test"), mail(1, b, a)]
        self.bots = ["a", "b"]

    def runner(self, argv):
        self.calls.append(argv)
        if self.fail_at in argv:
            return response(stderr="unavailable", code=1)
        if argv[0] == "test-orca":
            if argv[1:] == ["bot", "list", "--json"]:
                result = {"bots": [{"name": name} for name in self.bots]}
            elif argv[1:3] == ["bot", "chats"]:
                name = argv[4]
                self.assertEqual(argv[1:], ["bot", "chats", "--bot", name, "--all", "--json"])
                result = {"chats": [{"id": {"a": "alpha", "b": "beta"}[name]}]}
            else:
                self.assertEqual(argv[1:], ["orchestration", "inbox", "--json", "--full", "--limit", "1000"])
                result = {"messages": self.inbox}
            return response(json.dumps({"ok": True, "result": result}))
        if argv == ["herdr", "api", "snapshot"]:
            snap = {"panes": [dict(pane_id="w1:p1", workspace_id="demo", label="1")]}
            return response(json.dumps({"result": {"snapshot": snap}}))
        if argv[:3] == ["herdr", "pane", "process-info"]:
            return response("{}")
        if argv[:2] == ["tmux", "list-panes"]:
            return response("\x1f".join(["demo/1", "0", "2", "%2", "", "sh", "bad",
                                         "1", "1", "1", "0", "/example", ""]))
        if argv[0] == "ps":
            return response()
        if argv[:3] != ["herdr", "pane", "read"]:
            self.assertEqual(argv[:2], ["tmux", "capture-pane"])
        return response(self.capture)

    def invoke(self, *args, runner=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["read", *args], runner or self.runner)
        return code, out.getvalue(), err.getvalue()

    def records(self, *args):
        code, out, err = self.invoke("--format", "json", *args)
        self.assertEqual((code, err), (0, ""))
        return json.loads(out)

    def test_all_handles_runs_limit_order_and_full_body(self):
        self.inbox[1]["created_at"] = "2026-01-01T02:00:05+02:00"
        rows = self.records("--limit", "3", "orca:a")
        self.assertEqual(rows[-1]["timestamp"], "2026-01-01T00:00:05Z")
        self.assertEqual([r["id"] for r in rows], ["msg_2", "msg_4", "msg_5"])
        self.assertEqual([(r["from"], r["to"]) for r in rows],
                         [("bot:a", "dispatch:test"), ("bot:b", "bot:a"), ("term_one", "bot:a")])
        self.assertEqual(rows[-1]["body"], "line 5\n次")
        self.assertEqual(len(self.calls), 4)
        self.assertEqual([argv[4] for argv in self.calls[1:3]], ["a", "b"])
        self.inbox = [mail(i % 10, "orca_session_id:alpha", None) for i in range(25)]
        self.assertEqual(len(self.records("orca:a")), 20)

    def test_schema_and_terminal_commands(self):
        for target, canonical, command in [
            ("herdr:w1:p1", "herdr:demo/1", ["herdr", "pane", "read", "w1:p1", "--source", "recent",
                                             "--lines", "2", "--format", "text"]),
            ("tmux:%2", "tmux:demo/1:0.2", ["tmux", "capture-pane", "-p", "-J", "-t", "%2", "-S", "-2"]),
        ]:
            row, = self.records("--limit", "2", target)
            self.assertEqual(self.calls[-1], command)
            self.assertEqual(set(row), FIELDS)
            self.assertEqual(row["target"], canonical)
            self.assertEqual(row["from"], canonical)
            self.assertIs(row["structured"], False)
            self.assertEqual(row["body"], "\n日本\x1b[31m語\x1b[0m")
            self.assertTrue(row["timestamp"].endswith("Z"))
            for key in ("to", "id", "subject", "thread_id"):
                self.assertIsNone(row[key])
        self.assertEqual(set(self.records("orca:a")[0]), FIELDS)
        self.records("demo/1:0.2")
        self.assertEqual(self.calls[-1][-1], "-50")

    def test_terminal_table_and_empty(self):
        self.capture = "Hi\x1b]8;;url\x1b\\ link\x1b]8;;\x07\n\t\x01\x1b[31mred\x1b[0m\x7f\n"
        code, out, _ = self.invoke("tmux:%2")
        self.assertEqual(code, 0)
        self.assertTrue(out.endswith("  terminal-output  tmux:demo/1:0.2\n   Hi link\n   \tred\n"))
        self.capture = "\n\n"
        self.assertEqual(self.invoke("tmux:%2")[:2], (0, "No output.\n"))
        self.assertEqual(self.records("tmux:%2")[0]["body"], "")
        self.inbox = []
        self.assertEqual(self.invoke("orca:a")[:2], (0, "No messages.\n"))
        self.assertEqual(self.records("orca:a"), [])

    def test_table_removes_direction_controls_but_json_preserves_them(self):
        self.capture = "本文 \u2067abc\u2069 \u202edef\u202c 👩\u200d💻"
        self.assertEqual(self.records("tmux:%2")[0]["body"], self.capture)
        code, out, _ = self.invoke("tmux:%2")
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[-1], "   本文 abc def 👩\u200d💻")

    def test_invalid_z_timestamp_is_a_source_error(self):
        self.inbox[1]["created_at"] = "invalidZ"
        code, out, err = self.invoke("orca:a")
        self.assertEqual((code, out), (3, ""))
        self.assertIn("invalid Orca timestamp", err)

    def test_resolution_and_errors(self):
        code, _, err = self.invoke("orca:absent")
        self.assertEqual(code, 4)
        self.assertIn("orca:b", err)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.invoke("demo/1")[0], 5)
        self.assertEqual(self.invoke("a")[0], 4)
        self.fail_at = "capture-pane"
        self.assertEqual(self.invoke("tmux:%2")[0], 6)
        for stage in ("list", "chats", "inbox"):
            self.fail_at = stage
            self.assertEqual(self.invoke("orca:a")[0], 3)
        self.assertEqual(self.invoke("orca:a", runner=lambda argv: response("{"))[0], 3)

    def test_bad_limit_is_argument_error(self):
        for limit in ("0", "1001", "no"):
            with self.assertRaises(SystemExit) as error:
                self.invoke("--limit", limit, "orca:a")
            self.assertEqual(error.exception.code, 2)
        self.assertFalse(self.calls)


class CaptureDecodingTests(unittest.TestCase):
    def test_non_utf8_terminal_bytes_do_not_fail_the_read(self):
        import sys
        from agent_sessions import read
        argv = [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'ok\\n\\xff\\xfe bad\\n')"]
        result = read.capture(argv)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.split("\n")[0], "ok")
        self.assertIn("�", result.stdout)
