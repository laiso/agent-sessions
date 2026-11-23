import socket
import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from support import response
from agent_sessions.model import SourceError
from agent_sessions.tmux import collect, read_child


def record(win="1", idx="0", title="", current="bash", dead="0", active="1", attached="2"):
    return "\x1f".join(("dev", win, idx, "%12", title, current, "123", dead, active,
                        "1", attached, "/tmp/dev", "321:123456:123"))


class TmuxTests(unittest.TestCase):
    def read(self, *records, argv=()):
        def runner(command):
            self.assertEqual(command[:-1], ["tmux", "list-panes", "-a", "-F"])
            self.assertEqual(len(command[-1].split("\x1f")), 13)
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

    def test_shell_default_titles_hidden(self):
        host = socket.gethostname()
        for title in (f"kk@{host}:/tmp/dev", f"kk@{host}", f"root@{host} ~"):
            row, = self.read(record(title=title))
            self.assertEqual(row.pane, "1.0")
        row, = self.read(record(title=f"kk@{host}-other:/x"))
        self.assertEqual(row.pane, f"1.0 kk@{host}-other:/x")

    def test_read_child_falls_back_to_own_cmdline(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = Path(tmp)
            (proc / "123/task/123").mkdir(parents=True)
            (proc / "123/task/123/children").touch()
            (proc / "123/cmdline").write_bytes(b"sleep\000300\000")
            with patch("agent_sessions.tmux.Path", return_value=proc):
                self.assertEqual(read_child("123"), ["sleep", "300"])
                self.assertEqual(read_child("not-a-pid"), [])
                self.assertEqual(read_child("999999999"), [])

    def test_darwin_ps_snapshot(self):
        panes = record() + "\n" + record(idx="1").replace("\x1f123\x1f", "\x1f55\x1f")
        ps = "  123   1 bash\n 1010 123 node   /bin/claude --resume\n 99 123 vim\n 2010 1010 git\n 55 1 sleep 300"
        runner = Mock(side_effect=[response(panes), response(ps)])
        rows = collect(runner, system="Darwin")
        self.assertEqual([(r.command, r.agent) for r in rows],
                         [("node   /bin/claude --resume", "claude"), ("sleep 300", None)])
        self.assertEqual(runner.call_count, 2)
        self.assertEqual(runner.call_args.args[0], ["ps", "-A", "-ww", "-o", "pid=,ppid=,args="])

    def test_darwin_ps_fallback(self):
        for bad in (response(code=1), FileNotFoundError(), UnicodeError(), response("123 1 codex\nbad"),
                    response("33 1 unrelated")):
            with self.subTest(bad=bad):
                runner = Mock(side_effect=[response(record()), bad])
                row, = collect(runner, system="Darwin")
                self.assertEqual((row.command, row.status), ("bash", "shell"))

    def test_unknown_platform_uses_tmux_name(self):
        runner = Mock(return_value=response(record()))
        with patch("agent_sessions.tmux.read_child", side_effect=AssertionError):
            row, = collect(runner, system="Windows")
        self.assertEqual(row.command, "bash")
        runner.assert_called_once()

    def test_agent_name_only_from_executable_or_script(self):
        row, = self.read(record(), argv=["vim", "claude"])
        self.assertIsNone(row.agent)
        row, = self.read(record(), argv=["npx", "-y", "codex"])
        self.assertEqual(row.agent, "codex")
        row, = self.read(record(), argv=["/usr/bin/gemini", "--yolo"])
        self.assertEqual(row.agent, "gemini")

    def test_interpreter_options_that_take_arguments(self):
        cases = [
            (["node", "--require", "/tmp/x/claude", "/tmp/x/app.js"], None),
            (["node", "--require", "/tmp/x/hook.js", "/tmp/x/claude"], "claude"),
            (["node", "--require=/tmp/x/hook.js", "/tmp/x/codex", "--yolo"], "codex"),
            (["node", "-e", "require('claude')"], None),
            (["python3", "-m", "aider", "--model", "x"], "aider"),
            (["python3", "-W", "ignore", "/usr/bin/aider"], "aider"),
            (["python3", "-c", "import claude"], None),
            (["npx", "-p", "claude", "other-tool"], None),
            (["uvx", "--from", "x", "aider"], "aider"),
            (["uv", "run", "aider"], "aider"),
        ]
        for argv, expected in cases:
            with self.subTest(argv=argv):
                row, = self.read(record(), argv=argv)
                self.assertEqual(row.agent, expected)

    def test_explicit_title_with_host_prefix_kept(self):
        host = socket.gethostname()
        for title in (f"user@{host} deploy review", f"user@{host} release"):
            row, = self.read(record(title=title))
            self.assertEqual(row.pane, "1.0 " + title)
        for title in (f"user@{host} ~/src", f"user@{host} /tmp"):
            row, = self.read(record(title=title))
            self.assertEqual(row.pane, "1.0")

    def test_interpreter_review_round_two(self):
        cases = [
            (["node", "--eval=setInterval(()=>{},1)", "claude"], None),
            (["python3", "-cimport time", "codex"], None),
            (["python3", "-maider"], "aider"),
            (["deno", "run", "-c", "deno.json", "/x/claude"], "claude"),
            (["bun", "-c", "bunfig.toml", "/x/claude"], "claude"),
            (["npx", "--workspace", "claude", "other-tool"], None),
            (["uv", "run", "--with-requirements", "requirements.txt", "aider"], "aider"),
            (["uv", "run", "tool", "claude"], None),
        ]
        for argv, expected in cases:
            with self.subTest(argv=argv):
                row, = self.read(record(), argv=argv)
                self.assertEqual(row.agent, expected)

    def test_default_title_with_spaces_in_path_hidden(self):
        host = socket.gethostname()
        row, = self.read(record(title=f"user@{host} /tmp/project files"))
        self.assertEqual(row.pane, "1.0")
