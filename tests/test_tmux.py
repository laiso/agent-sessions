import socket
import unittest

from support import response
from agent_sessions.model import SourceError
from agent_sessions.tmux import collect


def record(win="1", idx="0", title="", current="bash", dead="0", active="1", attached="2"):
    return "\x1f".join(("dev", win, idx, "%12", title, current, "123", dead, active,
                        "1", attached, "/tmp/dev"))


class TmuxTests(unittest.TestCase):
    def read(self, *records, argv=()):
        def runner(command):
            self.assertEqual(command[:-1], ["tmux", "list-panes", "-a", "-F"])
            self.assertEqual(len(command[-1].split("\x1f")), 12)
            return response("\n".join(records) + "\n")

        def proc_reader(pid):
            self.assertEqual(pid, "123")
            return list(argv)

        return collect(runner, proc_reader)

    def test_session_detail_and_interpreter(self):
        row, = self.read(record(title="作業"), argv=["node", "/dir with spaces/claude", "--resume"])
        self.assertEqual((row.pane, row.agent, row.status, row.focused),
                         ("1.0 作業", "claude", "running", True))
        self.assertEqual((row.pane_id, row.command, row.cwd),
                         ("%12", "node /dir with spaces/claude --resume", "/tmp/dev"))

    def test_dead_precedes_agent(self):
        row, = self.read(record(dead="1", attached="0"), argv=["/bin/codex"])
        self.assertEqual((row.status, row.agent, row.focused), ("dead", "codex", False))

    def test_shell_host_and_fallback(self):
        row, = self.read(record(title=socket.gethostname(), current="/bin/zsh", active="0"))
        self.assertEqual((row.pane, row.command, row.status, row.focused),
                         ("1.0", "/bin/zsh", "shell", False))

    def test_non_agent_numeric_order(self):
        rows = self.read(record(win="10"), record(win="2", idx="12"), record(win="2", idx="3"),
                         argv=["my-codex"])
        self.assertEqual([r.pane for r in rows], ["2.3", "2.12", "10.0"])
        self.assertTrue(all(r.agent is None and r.status == "running" for r in rows))

    def test_absent_server(self):
        for reason in ("no server running on /tmp/tmux", "error connecting to /tmp/tmux"):
            self.assertEqual(collect(lambda cmd: response(stderr=reason, code=1)), [])

    def test_invalid_output(self):
        with self.assertRaises(SourceError):
            self.read("bad record")
