import json
import subprocess
import unittest

from support import fixture, response
from agent_sessions.herdr import collect
from agent_sessions.model import SourceError


class HerdrTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = fixture("herdr_snapshot")
        self.data = self.snapshot["result"]["snapshot"]

    def runner(self, argv):
        if argv == ["herdr", "api", "snapshot"]:
            return response(json.dumps(self.snapshot))
        self.assertEqual(argv[:-1], ["herdr", "pane", "process-info", "--pane"])
        if argv[-1] == "wP:p4":
            raise subprocess.TimeoutExpired(argv, 5)
        kind = "agent" if argv[-1] == "wN:pH" else "shell"
        return response(json.dumps(fixture("herdr_process_info_" + kind)))

    def test_fixture_mapping_order_and_partial_failure(self):
        self.data["panes"].reverse()
        first, shell, last = collect(self.runner)
        self.assertEqual((first.session, first.pane, first.agent, first.status, first.command),
                         ("wsh-2027-pre", "1", "agy", "idle", "agy --dangerously-skip-permissions"))
        self.assertEqual((shell.pane, shell.agent, shell.command), ("2", None, "/usr/bin/bash"))
        self.assertTrue(last.focused)
        self.assertIsNone(last.command)

    def test_display_fields_and_disambiguation(self):
        a, b, c = self.data["panes"]
        a.update(label="shared", title="unused", display_agent="AGY", foreground_cwd="/foreground")
        b.update(title="shared", agent_status="blocked")
        c.update(label="shared", agent_status="done")
        first, second, last = collect(self.runner)
        self.assertEqual((first.agent, first.cwd), ("AGY", "/foreground"))
        self.assertEqual([x.pane for x in [first, second, last]], ["shared", "shared#wN:pS", "shared"])
        self.assertEqual([second.status, last.status], ["blocked", "done"])

    def test_only_required_fields(self):
        self.snapshot["result"]["snapshot"] = {"panes": [{"pane_id": "p", "workspace_id": "w"}]}
        row, = collect(self.runner)
        self.assertEqual((row.session, row.pane, row.agent, row.status, row.cwd, row.focused),
                         ("w", "p", None, "unknown", None, False))

    def test_invalid_snapshot(self):
        for raw in ("{", "null", "{}", '{"result":{"snapshot":{"panes":[{}]}}}'):
            with self.subTest(raw=raw), self.assertRaises(SourceError):
                collect(lambda argv: response(raw))

    def test_invalid_process_response(self):
        for raw in ("{", "null", '{"result":{"process_info":{"foreground_processes":[]}}}'):
            with self.subTest(raw=raw):
                rows = collect(lambda argv: self.runner(argv) if argv[1] == "api" else response(raw))
                self.assertTrue(all(row.command is None for row in rows))
