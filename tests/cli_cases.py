"""Shared setup for CLI regression cases using the independent suite's fake tools."""

import json
import unittest
from textwrap import indent

from tests.helpers import FakeEnvironment, run_cli


def pane(**updates):
    fields = dict(
        session_name="demo",
        window_index=0,
        pane_index=0,
        pane_id="%1",
        pane_title="",
        pane_current_command="sh",
        pane_pid="999999",
        pane_dead=0,
        pane_active=1,
        window_active=1,
        session_attached=0,
        pane_current_path="/example",
        pid=10,
        start_time=100,
    )
    return {**fields, **updates}


class CliCase(unittest.TestCase):
    def setUp(self):
        self.fake = FakeEnvironment()
        self.addCleanup(self.fake.cleanup)
        self.extra = {}
        self.instrumentation = None

    def cli(self, *args, code=0):
        result = run_cli(list(args), env=self.fake.env(self.extra), instrumentation=self.instrumentation)
        self.assertEqual(result.returncode, code, result.stderr)
        return result

    def rows(self, *args):
        return json.loads(self.cli(*args, "--format", "json").stdout)

    def customize_host(self, code):
        # Only the product Python process receives the emulated host settings.
        setup = "import sys\nif sys.orig_argv[2:4] == ['-m', 'agent_sessions']:\n"
        (self.fake.root / "sitecustomize.py").write_text(setup + indent(code, "    "))
        self.extra["PYTHONDONTWRITEBYTECODE"] = "1"
        self.instrumentation = self.fake.root

    def intercept(self, tool, handler):
        """Handle selected fake-tool calls, forwarding the rest to its original stub."""
        delegate = tool.with_suffix(".original")
        if not delegate.exists():
            tool.rename(delegate)
        log = self.fake.state_dir / (tool.name + "-intercept.jsonl")
        self.fake.install_script(
            tool.name,
            f"""
import json, os, sys
argv = sys.argv[1:]
with open({str(log)!r}, "a") as stream:
    print(json.dumps(argv), file=stream)
{handler}
os.execv({str(delegate)!r}, [{str(delegate)!r}, *argv])
""",
        )
        return log

    def calls(self, log):
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
