"""Exercise delivery outcomes on isolated servers with disposable cat panes."""

import os
import shutil
import time
import unittest

from tests.helpers import FakeEnvironment, run_cli
from test_live_spec import IsolatedTmuxServer


class PasteTests(unittest.TestCase):
    def setUp(self):
        if not shutil.which("tmux"):
            if os.environ.get("E2E_REQUIRE_ALL") == "1":
                self.fail("tmux is required")
            self.skipTest("tmux is unavailable")
        self.server = self.enterContext(IsolatedTmuxServer())
        self.server.tmux("new", "-d", "-s", "safe", "cat")
        self.server.tmux("set-buffer", "-b", "keep", "existing")
        self.fake = self.enterContext(FakeEnvironment())
        self.env = dict(self.server.env, PATH=f"{self.fake.bin_dir}:{self.server.env['PATH']}")

    def wrapper(self, before="", after=""):
        self.fake.install_script(
            "tmux",
            f"""
import os,subprocess,sys
argv=sys.argv[1:]
tmux={shutil.which("tmux")!r}
{before}
result=subprocess.run([tmux,*argv],capture_output=True)
{after}
sys.stdout.buffer.write(result.stdout)
sys.stderr.buffer.write(result.stderr)
sys.exit(result.returncode)
""",
        )

    def send(self, *opts, code=0):
        res = run_cli(["send", *opts, "tmux:%0", "--", r"-例\;"], env=self.env)
        self.assertEqual(res.returncode, code, res.stderr)
        self.assertEqual(
            self.server.tmux("list-buffers", "-F", "#{buffer_name}").stdout.strip(), "keep"
        )
        self.assertEqual(self.server.tmux("show-buffer", "-b", "keep").stdout.strip(), "existing")
        return res.stderr

    def test_synchronized_delivery(self):
        other = self.server.tmux(
            "split-window", "-P", "-F", "#{pane_id}", "-t", "%0", "cat"
        ).stdout.strip()
        self.server.tmux("set-window-option", "synchronize-panes", "on")
        self.send("--no-enter")
        self.send()
        deadline = time.monotonic() + 2
        while True:
            seen = self.server.tmux("capture-pane", "-p", "-t", "%0").stdout.splitlines()
            if seen.count(r"-例\;-例\;") == 2:
                break
            if time.monotonic() >= deadline:
                self.fail("the target cat pane did not echo the submitted line")
            time.sleep(0.02)
        self.assertEqual(
            self.server.tmux(
                "display-message", "-p", "-t", other, "#{cursor_y}:#{cursor_x}"
            ).stdout,
            "0:0\n",
        )

    def test_changed_server(self):
        for enter in (False, True):
            with self.subTest(enter=enter), IsolatedTmuxServer() as fresh:
                fresh.tmux("new", "-d", "-s", "replacement", "cat")
                self.wrapper(
                    before=f"""
if argv[0]=='delete-buffer' or (argv[0]=='set-buffer' and (argv[4]=='\\n')=={enter!r}):
    os.environ['TMUX_TMPDIR']={fresh.tmpdir!r}
"""
                )
                detail = self.send(code=6)
                self.assertEqual(fresh.tmux("list-buffers", "-F", "#{buffer_name}").stdout, "")
                self.assertIn("target pane changed", detail)
                self.assertIn("text sent" if enter else "nothing sent", detail)
                self.assertEqual(
                    fresh.tmux(
                        "display-message", "-p", "-t", "%0", "#{cursor_y}:#{cursor_x}"
                    ).stdout,
                    "0:0\n",
                )

    def test_lost_reply_after_paste(self):
        self.wrapper(after="if argv[0]=='set-buffer': sys.exit(1)")
        self.assertIn("text delivery not confirmed", self.send(code=6))
        self.assertIn(r"-例\;", self.server.tmux("capture-pane", "-p", "-t", "%0").stdout)

    def test_disabled_input_and_hooks(self):
        self.server.tmux("set-hook", "-g", "after-set-buffer", "display-message -p notice")
        self.server.tmux("select-pane", "-d", "-t", "%0")
        self.wrapper(
            after="if argv[0]=='set-buffer': subprocess.run([tmux,'select-pane','-e','-t','%0'],check=True)"
        )
        detail = self.send(code=6)
        self.assertIn("nothing sent", detail)
        self.assertIn("input disabled", detail)
        self.assertEqual(
            self.server.tmux("display-message", "-p", "-t", "%0", "#{cursor_y}:#{cursor_x}").stdout,
            "0:0\n",
        )
        self.server.tmux(
            "set-hook", "-g", "after-set-buffer", "display-message -p pane-unavailable"
        )
        self.send()
        self.assertIn(r"-例\;", self.server.tmux("capture-pane", "-p", "-t", "%0").stdout)
