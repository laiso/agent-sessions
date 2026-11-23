import json
import os
import subprocess
import unittest
from unittest.mock import patch

from support import fixture, response
from agent_sessions import orca
from agent_sessions.model import SourceError
from agent_sessions.runner import run


class OrcaTests(unittest.TestCase):
    def setUp(self):
        self.cli = "/test path/orca"
        self.env = patch.dict(os.environ, {"AGENT_SESSIONS_ORCA": self.cli})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.bots = fixture("orca_bot_list")
        self.ps = fixture("orca_worktree_ps")
        self.calls = []

    def runner(self, argv):
        self.calls.append(argv)
        if argv == [self.cli, "bot", "list", "--json"]:
            return response(json.dumps(self.bots))
        self.assertEqual(argv, [self.cli, "worktree", "ps", "--json"])
        return response(json.dumps(self.ps))

    def test_fixture_uses_bot_home_and_sorts(self):
        self.bots["result"]["bots"].reverse()
        self.ps["result"]["worktrees"].append(dict(bot="claude", botHome=False, path="/wrong", agents=[]))
        rows = orca.collect(self.runner)
        self.assertEqual([r.session for r in rows], ["claude", "codex", "gemini"])
        self.assertEqual([r.status for r in rows], ["working", "idle", "idle"])
        self.assertEqual(rows[0].cwd, "/home/kk/.orca/bots/orca")
        self.assertEqual(rows[0].specialty, self.bots["result"]["bots"][-1]["specialty"])
        self.assertEqual(rows[0].command, rows[0].specialty)
        self.assertEqual(rows[2].record(), dict(source="orca", session="gemini", pane="home",
                         pane_id=self.bots["result"]["bots"][0]["id"], status="idle", agent="antigravity",
                         cwd="/home/kk/.orca/bots/gemini", command=None, focused=False, specialty=None))
        self.assertEqual(len(self.calls), 2)

    def test_state_priority(self):
        cases = [
            (True, ["working"], "paused"), (False, ["idle", "blocked", "working"], "working"),
            (False, ["idle", "blocked"], "blocked"), (False, ["done", "waiting"], "blocked"),
            (False, ["permission"], "blocked"), (False, ["idle"], "idle"), (False, ["done"], "idle"),
            (False, [], "stopped"), (False, ["other"], "unknown"),
        ]
        for paused, states, expected in cases:
            with self.subTest(states=states):
                home = {"agents": [{"state": state} for state in states]}
                self.assertEqual(orca.status({"paused": paused}, home), expected)

    def test_missing_home_and_no_agent(self):
        self.ps = {"result": {"worktrees": []}}
        del self.bots["result"]["bots"][0]["agent"]
        rows = orca.collect(self.runner)
        self.assertIsNone(rows[0].agent)
        self.assertTrue(all(r.status == "unknown" and r.cwd is None for r in rows))

    def test_ps_failures_preserve_paused(self):
        self.bots["result"]["bots"][1]["paused"] = True
        for bad in (response(code=1), response("not JSON"), response('{"ok":false}'),
                    response('{"result":{"worktrees":null}}')):
            with self.subTest(bad=bad):
                rows = orca.collect(lambda argv: self.runner(argv) if argv[1] == "bot" else bad)
                self.assertEqual([r.status for r in rows], ["unknown", "paused", "unknown"])
                self.assertTrue(all(r.cwd is None for r in rows))

    def test_bad_bot_list_is_unavailable(self):
        for bad in (response(code=1), response("{"), response("null"), response('{"result":{"bots":[]}}'),
                    response('{"ok":1,"result":{"bots":[]}}'), response('{"ok":false}'),
                    response('{"ok":true,"result":{"bots":{}}}')):
            with self.subTest(bad=bad), self.assertRaises(SourceError):
                orca.collect(lambda argv: bad)
        for exc in (FileNotFoundError(), PermissionError(), subprocess.TimeoutExpired(["orca"], 15)):
            def fail(argv):
                raise exc
            with self.subTest(exc=exc), self.assertRaises(SourceError):
                orca.collect(fail)

    def test_no_cli(self):
        with patch.dict(os.environ, {}, clear=True), patch("shutil.which", return_value=None):
            with self.assertRaises(SourceError):
                orca.collect(self.runner)
        self.assertEqual(self.calls, [])

    def test_cli_discovery_order(self):
        paths = {"orca": "/bin/orca", "orca.AppImage": "/bin/orca.AppImage"}
        env = {"AGENT_SESSIONS_ORCA": "/override", "ORCA_CLI_COMMAND": "/shim"}
        self.assertEqual(orca.find_cli(env, paths.get), "/override")
        env["AGENT_SESSIONS_ORCA"] = ""
        self.assertEqual(orca.find_cli(env, paths.get), "/shim")
        self.assertEqual(orca.find_cli({}, paths.get), "/bin/orca")
        del paths["orca"]
        self.assertEqual(orca.find_cli({}, paths.get), "/bin/orca.AppImage")

    def test_orca_timeout_is_local_to_orca(self):
        with patch("subprocess.run", side_effect=self.subprocess_run) as mocked:
            orca.collect()
            run(["tmux", "list-panes"])
        self.assertEqual([c.kwargs["timeout"] for c in mocked.call_args_list], [15, 15, 5])

    def subprocess_run(self, argv, **kwargs):
        return self.runner(argv) if argv[0] == self.cli else response()
