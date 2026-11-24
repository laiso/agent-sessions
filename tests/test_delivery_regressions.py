import json
from tests.cli_cases import CliCase, pane
from tests.helpers import LIST_SCHEMA_KEYS, sample_herdr_snapshot


class DeliveryTests(CliCase):
    def test_explicit_orca_before_lookup(self):
        self.fake.install_tmux_stub()
        self.fake.install_herdr_stub()
        self.fake.install_orca_shim(self.fake.make_orca_script_body())
        r = self.cli("send", "--dry-run", "orca:codex", "hi", code=2)
        self.assertEqual(r.stdout, "")
        self.assertEqual(
            r.stderr,
            "agent-sessions: send: orca: targets are read-only; send supports herdr: and tmux: targets\n",
        )
        self.assertEqual(
            self.fake.tmux_calls() + self.fake.herdr_calls() + self.fake.orca_calls(), []
        )

    def test_reject_before_lookup(self):
        self.fake.install_tmux_stub()
        for msg in (
            "",
            " ",
            "a\tb",
            "a\nb",
            "a\rb",
            "a\x1b",
            "a\x7f",
            "a\x85",
            "a\u2028",
            "あ" * 4097,
        ):
            self.cli("send", "tmux:demo", msg, code=2)
        self.assertEqual(self.fake.tmux_calls(), [])

    def test_pane_receipt_and_payload(self):
        self.fake.install_tmux_stub(panes=[pane()])
        (row,) = self.rows("list", "--source", "tmux")
        self.assertEqual(set(row), LIST_SCHEMA_KEYS)
        preview = self.rows("send", "--dry-run", "tmux:demo", "a b", " c ")
        self.assertEqual(
            preview, dict(sent=False, dry_run=True, enter=True, target=row, message="a b  c ")
        )
        self.assertTrue(all(call[0] == "list-panes" for call in self.fake.tmux_calls()))
        for text in ("Enter; echo $HOME", "a;", ";", "b\\;", "a ; b", "--flag"):
            result = self.cli("send", "--format", "json", "--no-enter", "tmux:%1", "--", text)
            self.assertEqual(
                json.loads(result.stdout),
                dict(sent=True, dry_run=False, enter=False, target=row, message=text),
            )
            cmd = self.fake.tmux_calls()[-1]
            self.assertEqual(cmd[3:5], ["--", text[:-1] + "\\;" if text.endswith(";") else text])
        self.cli("send", "tmux:%1", "--", "-abc")
        text, enter = self.fake.tmux_calls()[-2:]
        self.assertEqual([text[4], enter[4]], ["-abc", "\n"])
        self.assertNotEqual(text[2], enter[2])
        for cmd in (text, enter):
            buf = cmd[2]
            self.assertRegex(buf, r"^agent-sessions-[0-9a-f]{32}$")
            self.assertEqual(cmd[:2], ["set-buffer", "-b"])
            self.assertEqual(cmd[5:10], [";", "if-shell", "-F", "-t", "%1"])
            self.assertIn(f"paste-buffer -d -b {buf} -t %1", cmd[11])

    def test_herdr_delivery_stages(self):
        tool = self.fake.install_herdr_stub(snapshot=sample_herdr_snapshot())
        send_ok = "if argv[:2] in (['pane','send-text'], ['pane','send-keys']): sys.exit(0)"
        log = self.intercept(tool, send_ok)
        for text in (
            "--help",
            "-hello",
            "--",
            "--session=test",
            "--remote",
            "--remote-keybindings=local",
        ):
            start = len(self.calls(log))
            self.cli("send", "--no-enter", "herdr:w1:p1", "--", text)
            calls = [c for c in self.calls(log)[start:] if c[:2] == ["pane", "send-text"]]
            self.assertEqual("".join(c[3] for c in calls), text)
            self.assertTrue(all(c[:3] == ["pane", "send-text", "w1:p1"] for c in calls))
            if text.startswith(("--session", "--remote")):
                self.assertEqual(calls[0][-1], "-")
        self.cli("send", "herdr:my-app/1", "hello", "world")
        self.assertEqual(
            self.calls(log)[-2:],
            [
                ["pane", "send-text", "w1:p1", "hello world"],
                ["pane", "send-keys", "w1:p1", "enter"],
            ],
        )
        for fail, msg, reason, count in [
            ("hi", "hi", "pane unavailable", 1),
            ("-session=test", "--session=test", "leading '-' sent", 2),
            ("enter", "hi", "text sent, Enter failed", 2),
        ]:
            fail_code = f"if argv[-1] == {fail!r}:\n    print(json.dumps({{'error': {{'message': 'pane unavailable'}}}})); sys.exit(1)\n"
            self.intercept(tool, fail_code + send_ok)
            start = len(self.calls(log))
            result = self.cli("send", "herdr:w1:p1", "--", msg, code=6)
            self.assertEqual(result.stdout, "")
            self.assertIn(reason, result.stderr)
            self.assertEqual(
                len([c for c in self.calls(log)[start:] if c[1].startswith("send-")]), count
            )

    def test_tmux_failure_stages(self):
        tool = self.fake.install_tmux_stub(panes=[pane()])
        cases = [
            ("hi", "", 0, "", "text delivery not confirmed"),
            ("hi", "pane-unavailable", 0, "", "text delivery not confirmed"),
            ("hi", "{buf}-refused\n{buf}-pasted", 0, "", "text delivery not confirmed"),
            ("hi", "{buf}-refused", 1, "server exited", "text delivery not confirmed"),
            ("hi", "notice\n{buf}-refused", 0, "cleanup failed", "nothing sent"),
            ("\n", "{buf}-refused", 0, "cleanup failed", "text sent, Enter not sent"),
            ("\n", "", 1, "no buffer space available", "text sent, Enter delivery not confirmed"),
            ("hi", "", 1, "unknown buffer: {buf}", "text delivery not confirmed"),
        ]
        for phase, reply, rc, cleanup, reason in cases:
            with self.subTest(phase=phase, reply=reply, cleanup=cleanup):
                fault = f"""
if argv[0] == 'set-buffer' and argv[4] == {phase!r}:
    print({reply!r}.format(buf=argv[2]))
    sys.exit({rc})
if argv[0] == 'delete-buffer' and {bool(cleanup)!r}:
    print({cleanup!r}.format(buf=argv[2]), file=sys.stderr)
    sys.exit(1)
"""
                log = self.intercept(tool, fault)
                start = len(self.calls(log))
                result = self.cli("send", "tmux:%1", "hi", code=6)
                self.assertIn(reason, result.stderr)
                self.assertEqual(
                    "may remain" in result.stderr,
                    bool(cleanup and not cleanup.startswith("unknown buffer:")),
                )
                calls = self.calls(log)[start + 1 :]
                self.assertEqual(
                    [c[0] for c in calls],
                    ["set-buffer"] * (1 if phase == "hi" else 2) + ["delete-buffer"],
                )
                self.assertEqual(calls[-1][2], calls[-2][2])

    def test_enter_timeout_diagnostic(self):
        tool = self.fake.install_tmux_stub(panes=[pane()])
        self.intercept(
            tool,
            "if argv[0]=='delete-buffer': print('cleanup error\\ndetail',file=sys.stderr); sys.exit(1)",
        )
        self.customize_host("""import subprocess
original=subprocess.run
def deliver(argv,**kwargs):
    if argv[:2]==['tmux','set-buffer'] and argv[5]=='\\n':
        raise subprocess.TimeoutExpired(argv,kwargs['timeout'])
    return original(argv,**kwargs)
subprocess.run=deliver
""")
        result = self.cli("send", "tmux:%1", "private-message", code=6)
        self.assertEqual(result.stdout, "")
        self.assertEqual(len(result.stderr.splitlines()), 1)
        for fragment in (
            "text sent, Enter delivery not confirmed",
            "timed out after 5 seconds",
            "may remain (cleanup error detail)",
        ):
            self.assertIn(fragment, result.stderr)
        for private in ("private-message", "10:100:999999"):
            self.assertNotIn(private, result.stderr)
