import unittest
from dataclasses import replace

import support
from agent_sessions.model import Session, TmuxSession
from agent_sessions.target import TargetError, resolve


class TargetTests(unittest.TestCase):
    herdr = Session("herdr", "a/b", "one", "w1:p3", None, "unknown", None, None, False)
    tmux = TmuxSession("tmux", "dev:server", "1.2 my title", "%2", None, "shell",
                       "bash", "/tmp", False, True)

    def test_herdr_addresses(self):
        for target in ("herdr:a/b/one", "a/b/one", "herdr:w1:p3"):
            self.assertIs(resolve(target, [self.herdr]), self.herdr)
        for name in ("foo/bar", "one#w1:p3"):
            pane = replace(self.herdr, pane=name)
            self.assertIs(resolve("herdr:a/b/" + name, [pane]), pane)

    def test_tmux_addresses(self):
        other = replace(self.tmux, active=False, pane="2.0", pane_id="%3")
        for target in ("tmux:dev:server", "dev:server", "tmux:%2", "dev:server:1.2",
                       "tmux:dev:server:1.2"):
            self.assertIs(resolve(target, [other, self.tmux]), self.tmux)
        self.assertIs(resolve("tmux:dev:server:2.0", [other, self.tmux]), other)

    def test_session_fallback_after_last_separator(self):
        pane = replace(self.herdr, session="a/b/one")
        self.assertIs(resolve("herdr:a/b/one", [pane]), pane)
        self.assertIs(resolve("herdr:a/b/one", [pane, self.herdr]), self.herdr)
        tmux = replace(self.tmux, session="dev:server:1.2")
        self.assertIs(resolve("tmux:dev:server:1.2", [tmux]), tmux)

    def test_ambiguity_lists_all_candidates(self):
        panes = [replace(self.herdr, pane=str(i)) for i in range(7)]
        with self.assertRaises(TargetError) as error:
            resolve("herdr:a/b", panes)
        self.assertEqual(error.exception.code, 5)
        self.assertEqual(str(error.exception).splitlines()[1:], ["candidates:", *(f"  herdr:a/b/{i}" for i in range(7))])

    def test_conflict_between_sources(self):
        tmux = replace(self.tmux, session="a/b/one")
        with self.assertRaises(TargetError) as error:
            resolve("a/b/one", [self.herdr, tmux])
        self.assertEqual(error.exception.code, 5)
        self.assertIn("tmux:a/b/one:1.2", str(error.exception))

    def test_missing_case_and_hint_limit(self):
        panes = [replace(self.herdr, pane=str(i)) for i in range(7)]
        with self.assertRaises(TargetError) as error:
            resolve("herdr:a/b/MISSING", panes)
        self.assertEqual(error.exception.code, 4)
        self.assertEqual(len(str(error.exception).splitlines()), 7)  # reason, heading, 5 hints
        with self.assertRaises(TargetError):
            resolve("herdr:A/b/one", [self.herdr])

    def test_missing_id_does_not_route_to_a_same_named_session(self):
        for target, row in (("tmux:%99", replace(self.tmux, session="%99")),
                            ("herdr:w1:p9", replace(self.herdr, session="w1:p9"))):
            with self.assertRaises(TargetError) as error:
                resolve(target, [row])
            self.assertEqual(error.exception.code, 4)

    def test_dead(self):
        for target in ("tmux:%2", "dev:server", "dev:server:1.2"):
            with self.assertRaises(TargetError) as error:
                resolve(target, [replace(self.tmux, status="dead")])
            self.assertEqual(error.exception.code, 4)
            self.assertIn("dead", str(error.exception))
