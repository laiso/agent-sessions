import shutil
import subprocess

from tests.cli_cases import CliCase, pane
from tests.helpers import CLI_PATH, sample_herdr_snapshot


class SecurityRegressionTests(CliCase):
    def test_launcher_uses_checkout_from_another_directory(self):
        cwd = self.fake.root
        (cwd / "agent_sessions.py").write_text("")  # Shadows -m if cwd is searched.
        (cwd / "sitecustomize.py").write_text("print('ambient import path')")
        result = subprocess.run([CLI_PATH, "--help"], cwd=cwd, env=self.fake.env({"PYTHONPATH": str(cwd)}),
                                capture_output=True, text=True, check=True)
        self.assertIn("usage: agent-sessions", result.stdout)
        self.assertNotIn("ambient import path", result.stdout)

    def test_launcher_keeps_colon_in_checkout_path(self):
        checkout = self.fake.root / "checkout:copy"
        checkout.mkdir()
        launcher = shutil.copy2(CLI_PATH, checkout)
        (checkout / "src").symlink_to(CLI_PATH.parent / "src", target_is_directory=True)
        result = subprocess.run([launcher, "read", "--help"], cwd=self.fake.home_dir,
                                env=self.fake.env(), capture_output=True, text=True, check=True)
        self.assertIn("usage: agent-sessions read", result.stdout)

    def test_incomplete_inventory_blocks_unqualified_operations(self):
        self.fake.install_script("herdr", "raise SystemExit(1)")
        self.fake.install_tmux_stub(panes=[pane()])
        for args in (["read", "demo"], ["send", "demo", "hello"], ["send", "--dry-run", "demo", "hello"]):
            result = self.cli(*args, code=3)
            self.assertIn("qualify the target", result.stderr)
            self.assertFalse(result.stdout)
        self.assertTrue(all(call[0] == "list-panes" for call in self.fake.tmux_calls()))
        self.assertEqual(self.rows("list")[0]["session"], "demo")
        self.cli("send", "--dry-run", "tmux:demo", "hello")

        self.fake.install_herdr_stub(snapshot=sample_herdr_snapshot())
        self.fake.install_tmux_stub(list_rc=1, list_stderr="error connecting to /tmp/test (Permission denied)")
        self.cli("send", "my-app/1", "hello", code=3)
        self.cli("send", "--dry-run", "herdr:my-app/1", "hello")

    def test_capture_refuses_reused_tmux_pane_id(self):
        self.fake.install_tmux_stub(panes=[pane()], capture_identity="20:200:999999",
                                    captures={"%1": (0, "other pane", "")})
        result = self.cli("read", "--format", "json", "tmux:%1", code=6)
        self.assertFalse(result.stdout)
        self.assertIn("identity", result.stderr)

    def test_capture_of_retained_dead_pane_with_empty_pid(self):
        self.fake.install_tmux_stub(panes=[pane(pane_dead=1, pane_pid="")],
                                    captures={"%1": (0, "finished", "")})
        self.assertEqual(self.rows("read", "tmux:%1")[0]["body"], "finished")

    def test_named_herdr_targets_reject_duplicate_delivery_id(self):
        panes = [dict(workspace_id=w, pane_id="w1:p1", label="one") for w in ("a", "b")]
        self.fake.install_herdr_stub(snapshot={"result": {"snapshot": {"panes": panes}}})
        for args in (["read", "herdr:a/one"], ["send", "herdr:b/one", "hello"]):
            self.assertIn("ambiguous", self.cli(*args, code=5).stderr)
        for call in self.fake.herdr_calls():
            self.assertNotIn(call[:2], (["pane", "read"], ["pane", "send-text"], ["pane", "send-keys"]))

    def test_backend_errors_are_safe_to_display(self):
        diagnostic = "first\x1b[31m\nsecond\u202e!"
        self.fake.install_tmux_stub(list_rc=1, list_stderr=diagnostic)
        listing = self.cli("list", "--source", "tmux", code=3)
        self.fake.install_tmux_stub(panes=[pane()], send_rc=1, send_stderr=diagnostic)
        sending = self.cli("send", "tmux:%1", "hello", code=6)
        for result in (listing, sending):
            self.assertIn("first [31m second !", result.stderr)
            self.assertNotIn("\x1b", result.stderr)
            self.assertNotIn("\u202e", result.stderr)
            self.assertEqual(len(result.stderr.splitlines()), 1)
