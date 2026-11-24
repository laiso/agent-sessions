import io
import json
import os
import subprocess
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from src.agent_sessions import orca, orca_sender
from src.agent_sessions.cli import main
from tests.helpers import LIST_SCHEMA_KEYS


class Service:
    def __init__(self):
        self.rows = {"term_existing": False}
        self.calls = []
        self.created = 0
        self.failure = None
        self.timeout = False

    def __call__(self, argv):
        _, group, command, *args = argv
        self.calls.append((command, args))
        if command == self.failure:
            return subprocess.CompletedProcess(argv, 1, "", "test failure")
        if (group, command) == ("bot", "list"):
            result = {"bots": [{"name": "sample", "id": "bot-id", "agent": "sample"}]}
        elif (group, command) == ("terminal", "list"):
            result = {"terminals": [dict(handle=h, orphaned=o, tabId="tab-" + h, title="dynamic") for h, o in self.rows.items()]}
        elif command == "create":
            assert args == ["--title", "agent-sessions", "--json"]
            time.sleep(.01)
            self.created += 1
            h = f"term_test{self.created}"
            self.rows[h] = False
            result = {"terminal": {"handle": h}}
        elif command == "close":
            assert args == ["--terminal", args[1], "--tab", "--json"] and args[1] != "term_existing"
            self.rows.pop(args[1])
            result = {}
        elif command == "send" and "--from" not in args:
            # Inside an Orca agent session: sent as that session, no terminal involved.
            assert args[:2] == ["--to", "bot:sample"]
            result = {"messageId": "session"}
        elif command == "send":
            assert args[:2] == ["--to", "bot:sample"] and args[2] == "--from"
            assert args[3].startswith("term_test") and self.rows[args[3]] is False
            if self.timeout:
                raise subprocess.TimeoutExpired(argv, 15)
            result = {"messageId": "test"}
        else:
            raise AssertionError(argv)
        return subprocess.CompletedProcess(argv, 0, json.dumps(dict(ok=True, result=result)), "")


class SenderTests(unittest.TestCase):
    def setUp(self):
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, XDG_STATE_HOME=tmp, AGENT_SESSIONS_ORCA="/example/cli"))
        for name in orca_sender.SESSION_VARIABLES:  # terminal mode unless a test opts in
            self.enterContext(patch.dict(os.environ, {name: ""}))
        self.state = Path(tmp) / "agent-sessions/orca-sender.json"
        self.service = Service()

    def cli(self, *argv, code=0):
        with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
            try:
                rc = main(list(argv), self.service)
            except SystemExit as exc:
                rc = exc.code
        self.assertEqual(rc, code, err.getvalue())
        return err.getvalue() if code else out.getvalue()

    def test_preview_and_input_errors(self):
        receipt = json.loads(self.cli("send", "--dry-run", "--format", "json", "orca:sample", "hi"))
        self.assertEqual(set(receipt), {"sent", "enter", "message", "target", "dry_run"})
        self.assertFalse(receipt["enter"] or receipt["sent"])
        self.assertTrue(receipt["dry_run"])
        self.assertEqual(set(receipt["target"]), LIST_SCHEMA_KEYS)
        self.assertIn("orca:sample", self.cli("send", "orca:missing", "hi", code=4))
        self.assertFalse(self.state.parent.exists())
        self.assertEqual(self.service.calls, [("list", ["--json"])] * 2)
        for args in (("--subject", "s", "tmux:test", "hi"), ("--orca-type", "status", "target", "hi"),
                     ("orca:sample", "a\nb"), ("--subject", "a\nb", "orca:sample", "hi")):
            self.cli("send", *args, code=2)
        self.assertEqual(len(self.service.calls), 2)
        self.service.failure = "list"
        self.cli("send", "orca:sample", "hi", code=3)

    def test_reuse_receipt_flags_and_close(self):
        self.assertIn("No saved", self.cli("orca-terminal", "status"))
        self.cli("orca-terminal", "close")
        text = "λ" * 61
        self.assertIn("Sent to orca sample (bot-id)", self.cli("send", "orca:sample", text))
        state = json.loads(self.state.read_text())
        self.assertEqual(set(state), {"cli", "handle", "created_at"})
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.state.parent.stat().st_mode & 0o777, 0o700)
        self.assertIn("available", self.cli("orca-terminal", "status"))
        receipt = json.loads(self.cli("send", "--format", "json", "--no-enter", "--orca-type", "status",
                                      "--subject=--to=bot:other", "orca:sample", "--", "--from=term_existing"))
        self.assertTrue(receipt["sent"])
        self.assertFalse(receipt["enter"])
        sends = [a for c, a in self.service.calls if c == "send"]
        self.assertEqual(sends[0][4:7], ["--type", "question", "--subject=agent-sessions: " + "λ"*60 + "…"])
        self.assertEqual(sends[1][4:8], ["--type", "status", "--subject=--to=bot:other", "--body=--from=term_existing"])
        self.assertEqual(self.service.created, 1)
        self.cli("orca-terminal", "close")
        self.assertFalse(self.state.exists())
        self.assertEqual(self.service.rows, {"term_existing": False})

    def test_unavailable_or_untrusted_state(self):
        self.cli("send", "orca:sample", "hi")
        self.service.rows["term_test1"] = True
        self.assertIn("orphaned", self.cli("orca-terminal", "status"))
        self.cli("send", "orca:sample", "hi")
        del self.service.rows["term_test2"]
        self.assertIn("missing", self.cli("orca-terminal", "status"))
        self.cli("send", "orca:sample", "hi")
        self.state.write_text("invalid")
        self.cli("send", "orca:sample", "hi")
        self.state.chmod(0o644)
        self.cli("send", "orca:sample", "hi")
        self.assertEqual(self.service.created, 5)
        del self.service.rows["term_test5"]
        self.cli("orca-terminal", "close")
        self.assertFalse(any(c == "close" for c, _ in self.service.calls))

    def test_concurrent_sends_and_failure(self):
        self.service.failure = "create"
        self.cli("send", "orca:sample", "hi", code=6)
        self.service.failure = None
        def deliver(_):
            orca_sender.manage("send", client=orca.reader(self.service), name="sample",
                               subject="test", kind="handoff", message="hello")
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(deliver, range(4)))
        self.assertEqual(self.service.created, 1)
        self.service.timeout = True
        self.assertIn("not confirmed", self.cli("send", "orca:sample", "hi", code=6))
        self.assertEqual(sum(c == "send" for c, _ in self.service.calls), 5)
        with patch.dict(os.environ, AGENT_SESSIONS_ORCA="/some/other"):
            self.cli("orca-terminal", "close", code=6)
        self.assertIn("term_test1", self.service.rows)

    def test_reject_existing_handle_create_and_shared_tab_close(self):
        def existing(argv):
            if argv[1:3] == ["terminal", "create"]:
                reply = {"ok": True, "result": {"terminal": {"handle": "term_existing"}}}
                return subprocess.CompletedProcess(argv, 0, json.dumps(reply), "")
            return self.service(argv)
        with self.assertRaisesRegex(orca.SourceError, "new sender terminal"):
            orca_sender.manage("send", client=orca.reader(existing), name="sample",
                               kind="question", subject="Hi", message="Hi")
        self.assertFalse(self.state.exists())
        self.cli("send", "orca:sample", "hi")
        def shared(argv):
            response = self.service(argv)
            if argv[1:3] == ["terminal", "list"]:
                data = json.loads(response.stdout)
                for row in data["result"]["terminals"]:
                    row["tabId"] = "shared"
                response.stdout = json.dumps(data)
            return response
        with self.assertRaisesRegex(orca.SourceError, "own tab"):
            orca_sender.manage("close", shared)
        self.assertTrue(self.state.exists())
        self.assertFalse(any(cmd == "close" for cmd, _ in self.service.calls))

    def test_o2_contract_with_synthetic_inbox(self):
        from tests_e2e.test_live_orca_sender import OrcaSenderLive
        env = {"E2E_ORCA_SEND_TO": "sample"}
        def service(argv):
            if argv[1:3] == ["orchestration", "inbox"]:
                send, = [args for cmd, args in self.service.calls if cmd == "send"]
                result = {"messages": [dict(from_handle=send[3], to_handle="run:sample",
                                            subject=send[6].removeprefix("--subject="),
                                            body=send[7].removeprefix("--body="))]}
                return subprocess.CompletedProcess(argv, 0, json.dumps(dict(ok=True, result=result)), "")
            if argv[1:3] == ["bot", "chats"]:
                return subprocess.CompletedProcess(argv, 0, '{"ok":true,"result":{"chats":[]}}', "")
            return self.service(argv)
        with patch.dict(os.environ, env), patch.object(orca, "run", lambda argv, **kw: service(argv)):
            case = OrcaSenderLive("test_O2_explicit_delivery")
            case.setUp()
            try:
                # The real E2E stays gated at discovery; exercise its body with a fake service.
                method = case.test_O2_explicit_delivery
                getattr(method, "__wrapped__", method.__func__)(case)
            finally:
                case.doCleanups()
        self.assertEqual(self.service.created, 1)
        self.assertEqual(self.service.rows, {"term_existing": False})


class SessionModeTests(SenderTests):
    """Inside an Orca agent session Orca refuses a different --from, so send as the session."""

    def test_session_send_uses_no_terminal_and_no_from(self):
        for name in orca_sender.SESSION_VARIABLES:
            with self.subTest(variable=name), patch.dict(os.environ, {name: "session-1"}):
                self.service.calls.clear()
                self.assertIn("Sent to orca sample (bot-id)", self.cli("send", "orca:sample", "hi"))
                sends = [args for command, args in self.service.calls if command == "send"]
                self.assertEqual(len(sends), 1)
                self.assertNotIn("--from", sends[0])
                self.assertIn("--type", sends[0])
                self.assertEqual(sends[0][sends[0].index("--type") + 1], "question")
                self.assertFalse(any(c in ("create", "close") or args == ["--json"] and c == "list" and False
                                     for c, args in self.service.calls))
                self.assertNotIn("create", [c for c, _ in self.service.calls])
                self.assertFalse(self.state.exists())

    def test_session_delivery_failure_is_exit_6(self):
        with patch.dict(os.environ, {"ORCA_AGENT_SESSION_ID": "session-1"}):
            self.service.failure = "send"
            self.assertIn("delivery not confirmed", self.cli("send", "orca:sample", "hi", code=6))
