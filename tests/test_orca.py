import json
import os
import subprocess
import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

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
            with patch("platform.system", return_value="Linux"), self.assertRaises(SourceError):
                orca.collect(self.runner)
        self.assertEqual(self.calls, [])

    def test_cli_discovery_order(self):
        env = {"AGENT_SESSIONS_ORCA": "/override", "ORCA_CLI_COMMAND": "/shim"}
        self.assertEqual(next(orca.cli_candidates(env), None), "/override")
        env["AGENT_SESSIONS_ORCA"] = ""
        self.assertEqual(next(orca.cli_candidates(env), None), "/shim")

    def test_orca_timeout_is_local_to_orca(self):
        with patch("subprocess.run", side_effect=self.subprocess_run) as mocked:
            orca.collect()
            run(["tmux", "list-panes"])
        self.assertEqual([c.kwargs["timeout"] for c in mocked.call_args_list], [15, 15, 5])

    def subprocess_run(self, argv, **kwargs):
        return self.runner(argv) if argv[0] == self.cli else response()


class PathDiscoveryTests(unittest.TestCase):
    def setUp(self):
        system = patch("platform.system", return_value="Linux")
        system.start()
        self.addCleanup(system.stop)
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
        self.shim.unlink()
        self.image.unlink()
        for data in (b"#!/bin/sh\nexit 0\n", b"#!/usr/bin/python3\nfrom orca import orca\n",
                     b"#!/usr/bin/python3\nfrom importlib import import_module\n", b"\x7fELF"):
            with self.subTest(data=data), patch.dict(os.environ, self.env, clear=True):
                self.executable(self.first / "orca", data)
                self.executable(self.last / "orca.AppImage", b"\x7fELF" + bytes(4) + b"AI\x02")
                with self.assertRaises(SourceError):
                    orca.collect(lambda argv: self.fail(f"unexpected command: {argv}"))

    def test_skip_unusable_shim_before_appimage(self):
        self.executable(self.first / "orca")
        (self.first / "orca.AppImage").symlink_to(self.image)
        (self.last / "orca").symlink_to(self.shim)
        self.assertEqual(next(orca.cli_candidates(self.env), None), str(self.shim))
        self.shim.chmod(0o600)
        self.assertEqual(next(orca.cli_candidates(self.env), None), str(self.image))

    def test_known_installations_without_path(self):
        self.env["PATH"] = ""
        self.assertEqual(list(orca.cli_candidates(self.env)), [str(self.shim), str(self.image)])

    def test_symlink_is_checked_using_target_mode(self):
        self.shim.unlink()
        self.shim.symlink_to(self.image)
        self.assertEqual(next(orca.cli_candidates(self.env)), str(self.shim))
        for mode in (0o775, 0o757, 0o600):
            with self.subTest(mode=mode):
                self.image.chmod(mode)
                self.assertEqual(list(orca.cli_candidates(self.env)), [])

    def test_unsafe_shim_only_uses_safe_appimage(self):
        self.shim.chmod(0o775)
        self.assertEqual(list(orca.cli_candidates(self.env)), [str(self.image)])
        self.image.chmod(0o777)
        self.assertEqual(list(orca.cli_candidates(self.env)), [])

    def test_candidate_ownership(self):
        for uid, allowed in ((1001, True), (0, True), (1002, False)):
            with self.subTest(uid=uid), patch("os.geteuid", return_value=1001), patch.object(
                Path, "stat", return_value=Mock(st_mode=0o100755, st_uid=uid)
            ):
                self.assertEqual(bool(list(orca.cli_candidates(self.env))), allowed)

    def test_unavailable_shim_retries_only_failed_command(self):
        for failed in ("bot", "worktree"):
            calls = []
            def runner(argv):
                calls.append(argv[:2])
                if argv[:2] == [str(self.shim), failed]:
                    return response(stderr="Orca CLI is unavailable; reopen Orca", code=1)
                key = "orca_bot_list" if argv[1] == "bot" else "orca_worktree_ps"
                return response(json.dumps(fixture(key)))
            with self.subTest(failed=failed), patch.dict(os.environ, self.env, clear=True):
                self.assertEqual(len(orca.collect(runner)), 3)
                self.assertEqual(calls[0], [str(self.shim), "bot"])
                self.assertEqual(calls[-1], [str(self.image), "worktree"])
                self.assertEqual(len(calls), 3)
                self.assertEqual(calls[1], [str(self.image), "bot"] if failed == "bot"
                                 else [str(self.shim), "worktree"])

    def test_retry_limit_and_explicit_override(self):
        for explicit, reason, count in ((False, "Orca CLI is unavailable", 2),
                                        (True, "Orca CLI is unavailable", 1), (False, "other error", 1)):
            env = dict(self.env)
            if explicit:
                env["ORCA_CLI_COMMAND"] = str(self.shim)
            runner = Mock(return_value=response(stderr=reason, code=1))
            with self.subTest(explicit=explicit, reason=reason), patch.dict(os.environ, env, clear=True):
                with self.assertRaises(SourceError):
                    orca.collect(runner)
                self.assertEqual(runner.call_count, count)

    def test_explicit_standalone_image_wins_over_path(self):
        image = self.first / "orca.AppImage"
        self.executable(image)
        (self.last / "orca").symlink_to(self.shim)
        self.env["ORCA_CLI_COMMAND"] = str(image)
        self.assertEqual(next(orca.cli_candidates(self.env), None), str(image))
        self.env["AGENT_SESSIONS_ORCA"] = "/nonexistent"
        self.assertEqual(next(orca.cli_candidates(self.env), None), "/nonexistent")

    def test_platform_installations(self):
        home = Path(self.env["HOME"])
        root = home / "root"
        bundle = "Applications/Orca Desktop.app/Contents/MacOS/orca"
        shim, system_app, user_app = home / ".local/bin/orca", root / bundle, home / bundle
        for path in (shim, system_app, user_app):
            path.parent.mkdir(parents=True)
            self.executable(path)

        def candidates(system):
            return list(orca.cli_candidates(self.env, system=system, root=root))

        self.assertEqual(candidates("Linux"), list(map(str, (self.shim, self.image))))
        self.assertEqual(candidates("Windows"), [])
        self.assertEqual(candidates("Darwin"), list(map(str, (shim, system_app))))
        system_app.chmod(0o777)
        self.assertEqual(candidates("Darwin"), list(map(str, (shim, user_app))))
        shim.chmod(0o600)
        self.assertEqual(candidates("Darwin"), [str(user_app)])
        self.env["ORCA_CLI_COMMAND"] = "/explicit"
        self.assertEqual(candidates("Windows"), ["/explicit"])

    def test_macos_stale_shim_only_retries_first_eligible_bundle(self):
        home = Path(self.env["HOME"])
        root = home / "root"
        bundle = "Applications/Orca Desktop.app/Contents/MacOS/orca"
        shim, app = home / ".local/bin/orca", root / bundle
        for path in (shim, app, home / bundle):
            path.parent.mkdir(parents=True)
            self.executable(path)

        candidates = orca.cli_candidates(self.env, system="Darwin", root=root)
        runner = Mock(side_effect=[response(stderr="Orca CLI is unavailable", code=1),
                                   response(json.dumps(fixture("orca_bot_list"))),
                                   response(stderr="Orca CLI is unavailable", code=1)])
        with patch.object(orca, "cli_candidates", return_value=candidates):
            self.assertEqual(len(orca.collect(runner)), 3)
        self.assertEqual([c.args[0] for c in runner.call_args_list],
                         [[str(shim), "bot", "list", "--json"], [str(app), "bot", "list", "--json"],
                          [str(app), "worktree", "ps", "--json"]])
