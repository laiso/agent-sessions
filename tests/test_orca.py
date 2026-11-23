import json
import os
import subprocess
import tempfile
from pathlib import Path
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
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {"HOME": home, "PATH": home}, clear=True):
            with self.assertRaises(SourceError):
                orca.collect(self.runner)
        self.assertEqual(self.calls, [])

    def test_cli_discovery_order(self):
        env = {"AGENT_SESSIONS_ORCA": "/override", "ORCA_CLI_COMMAND": "/shim"}
        self.assertEqual(orca.find_cli(env), "/override")
        env["AGENT_SESSIONS_ORCA"] = ""
        self.assertEqual(orca.find_cli(env), "/shim")

    def test_orca_timeout_is_local_to_orca(self):
        with patch("subprocess.run", side_effect=self.subprocess_run) as mocked:
            orca.collect()
            run(["tmux", "list-panes"])
        self.assertEqual([c.kwargs["timeout"] for c in mocked.call_args_list], [15, 15, 5])

    def subprocess_run(self, argv, **kwargs):
        return self.runner(argv) if argv[0] == self.cli else response()


class PathDiscoveryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name)
        self.first = home / "first"
        self.last = home / "last"
        self.first.mkdir()
        self.last.mkdir()
        self.env = {"HOME": str(home), "XDG_CONFIG_HOME": str(home / "config"),
                    "PATH": os.pathsep.join(map(str, (self.first, self.last)))}
        self.shim = home / "config/orca/linux-orca-cli-shim/orca"
        self.image = home / ".local/opt/orca/orca.AppImage"
        for path in (self.shim, self.image):
            path.parent.mkdir(parents=True)
            self.executable(path)

    @staticmethod
    def executable(path, data=b"#!/bin/sh\nexit 0\n"):
        path.write_bytes(data)
        path.chmod(0o700)

    def test_unknown_candidates_never_reach_runner(self):
        for data in (b"#!/bin/sh\nexit 0\n", b"#!/usr/bin/python3\nfrom orca import orca\n",
                     b"#!/usr/bin/python3\nfrom importlib import import_module\n", b"\x7fELF"):
            with self.subTest(data=data), patch.dict(os.environ, self.env, clear=True):
                self.executable(self.first / "orca", data)
                self.executable(self.last / "orca.AppImage", b"\x7fELF" + bytes(4) + b"AI\x02")
                with self.assertRaises(SourceError):
                    orca.collect(lambda argv: self.fail(f"unexpected command: {argv}"))

    def test_skip_unrecognized_first_orca_before_appimage(self):
        self.executable(self.first / "orca")
        (self.first / "orca.AppImage").symlink_to(self.image)
        (self.last / "orca").symlink_to(self.shim)
        self.assertEqual(orca.find_cli(self.env), str(self.shim))
        self.shim.chmod(0o600)
        self.assertEqual(orca.find_cli(self.env), str(self.image))

    def test_first_recognized_orca_in_path_order(self):
        (self.first / "orca").symlink_to(self.image)
        (self.last / "orca").symlink_to(self.shim)
        self.assertEqual(orca.find_cli(self.env), str(self.image))
        self.env["PATH"] = os.pathsep.join(map(str, (self.last, self.first)))
        self.assertEqual(orca.find_cli(self.env), str(self.shim))

    def test_registered_file_reached_through_symlink_chain(self):
        alias = self.last / "alias"
        alias.symlink_to(self.shim)
        (self.first / "orca").symlink_to(alias)
        calls = []
        with patch.dict(os.environ, self.env, clear=True):
            rows = orca.collect(lambda argv: calls.append(argv) or response('{"ok":true,"result":{"bots":[]}}'))
        self.assertEqual(rows, [])
        self.assertEqual(calls, [[str(self.shim), "bot", "list", "--json"]])
        self.shim.unlink()
        self.assertIsNone(orca.find_cli(self.env))

    def test_explicit_standalone_image_wins_over_path(self):
        image = self.first / "orca.AppImage"
        self.executable(image)
        (self.last / "orca").symlink_to(self.shim)
        self.env["ORCA_CLI_COMMAND"] = str(image)
        self.assertEqual(orca.find_cli(self.env), str(image))
        self.env["AGENT_SESSIONS_ORCA"] = "/nonexistent"
        self.assertEqual(orca.find_cli(self.env), "/nonexistent")
