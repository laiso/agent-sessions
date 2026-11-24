"""Live sender lifecycle. Only O2 can deliver mail to a real bot."""
import io
import json
import os
import subprocess
import tempfile
import unittest
import uuid
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from src.agent_sessions import orca, orca_sender
from src.agent_sessions.cli import main
from src.agent_sessions.model import SourceError
from src.agent_sessions.runner import run


def check_delivery(test, client, sender, name, subject, body):
    rows = client("messages", "orchestration", "inbox", "--json", "--full", "--limit", "1000")
    matches = [m for m in rows if m.get("subject") == subject and m.get("body") == body]
    test.assertEqual(len(matches), 1, "expected one matching message in the inbox")
    msg = matches[0]
    test.assertTrue(msg.get("from_handle") == sender, "unexpected message sender")
    chats = client("chats", "bot", "chats", "--bot", name, "--all", "--json")
    handles = {"orca_session_id:" + c["id"] for c in chats if isinstance(c.get("id"), str)}
    handles.add("bot:" + name)
    to = msg.get("to_handle") or ""
    test.assertTrue(to in handles or to.startswith("run:") and len(to) > 4, "unexpected recipient")


class OrcaSenderLive(unittest.TestCase):
    def setUp(self):
        state = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, XDG_STATE_HOME=state))
        self.state = Path(state) / "agent-sessions/orca-sender.json"
        try:
            self.client = orca.reader()
            self.bots = self.client("bots", "bot", "list", "--json")
        except SourceError:
            self.bots = []
        if not self.bots:
            if os.environ.get("E2E_REQUIRE_ALL") == "1":
                self.fail("live Orca bot required")
            self.skipTest("live Orca bot unavailable")
        self.before = self.inventory()
        self.addCleanup(self.cleanup_terminal)

    def inventory(self):
        return set(orca_sender.terminals(self.client))

    def cleanup_terminal(self):
        if self.state.exists():
            self.cli("orca-terminal", "close")
        self.assertTrue(self.inventory() == self.before, "terminal inventory changed after close")

    def cli(self, *argv, runner=None, code=0):
        with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
            self.assertEqual(main(list(argv), runner), code, err.getvalue())
        return out.getvalue()

    def test_O1_reuse_with_delivery_intercepted(self):
        sources = []
        def intercept(argv):
            if argv[1:3] == ["orchestration", "send"]:
                sources.append(argv[argv.index("--from") + 1])
                return subprocess.CompletedProcess(argv, 0, '{"ok":true,"result":{"messageId":"test"}}', "")
            return run(argv, timeout=15)
        # Exercise the plain-shell path only behind the delivery interceptor.
        # The real CLI inherits the original caller identity for every operation.
        with patch.object(orca_sender, "orca_session", return_value=False):
            for _ in range(2):
                self.cli("send", "orca:" + self.bots[0]["name"], "test", runner=intercept)
                self.assertTrue(self.inventory() == self.before | {sources[-1]})
        self.assertTrue(sources[0] == sources[1] and sources[0] not in self.before)

    @unittest.skipUnless(os.environ.get("E2E_ORCA_SEND_TO"), "real delivery requires E2E_ORCA_SEND_TO")
    def test_O2_explicit_delivery(self):
        name = os.environ["E2E_ORCA_SEND_TO"]
        subject = "agent-sessions delivery check " + uuid.uuid4().hex
        body = "Test delivery. No action required."
        self.cli("send", "--dry-run", "--subject", subject, "orca:" + name, body)
        self.cli("send", "--subject", subject, "orca:" + name, body)
        if orca_sender.orca_session():
            handle = os.environ.get("ORCA_TERMINAL_HANDLE", "")
            session = os.environ.get("ORCA_AGENT_SESSION_ID", "").strip()
            sender = ("orca_session_id:" + session
                      if session and not handle.startswith("structworker_") else handle)
            self.assertTrue(sender and not self.state.parent.exists())
            self.assertTrue(self.inventory() == self.before)
        else:
            sender = json.loads(self.state.read_text())["handle"]
            self.assertTrue(self.inventory() == self.before | {sender} and sender not in self.before)
        check_delivery(self, self.client, sender, name, subject, body)

    def test_O3_preview_and_O4_missing(self):
        self.cli("send", "--dry-run", "orca:" + self.bots[0]["name"], "test")
        self.assertTrue(self.inventory() == self.before and not self.state.exists())
        name = "missing-" + uuid.uuid4().hex
        self.assertFalse(any(bot["name"] == name for bot in self.bots))
        self.cli("send", "orca:" + name, "test", code=4)
        self.assertTrue(self.inventory() == self.before and not self.state.exists())
