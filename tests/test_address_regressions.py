from tests.cli_cases import CliCase, pane


class AddressTests(CliCase):
    def herdr(self, names, workspace="a/b"):
        panes = [
            dict(workspace_id=workspace, pane_id=f"w1:p{i}", label=name)
            for i, name in enumerate(names)
        ]
        self.fake.install_herdr_stub(snapshot={"result": {"snapshot": {"panes": panes}}})

    def target_id(self, target):
        return self.rows("send", "--dry-run", target, "hello")["target"]["pane_id"]

    def test_address_resolution(self):
        self.herdr(["first", "first", "foo/bar"])
        self.fake.install_tmux_stub(
            panes=[
                pane(session_name="dev:server", window_index=1, pane_index=2),
                pane(session_name="dev:server", window_index=2, pane_id="%2", pane_active=0),
            ]
        )
        for address in ("herdr:a/b/first", "a/b/first", "herdr:w1:p0"):
            self.assertEqual(self.target_id(address), "w1:p0")
        self.assertEqual(self.target_id("herdr:a/b/first#w1:p1"), "w1:p1")
        self.assertEqual(self.target_id("herdr:a/b/foo/bar"), "w1:p2")
        for address in (
            "tmux:dev:server",
            "dev:server",
            "tmux:%1",
            "dev:server:1.2",
            "tmux:dev:server:1.2",
        ):
            self.assertEqual(self.target_id(address), "%1")
        self.assertEqual(self.target_id("tmux:dev:server:2.0"), "%2")
        self.herdr(["one"], "a/b/first")
        self.assertEqual(self.target_id("herdr:a/b/first"), "w1:p0")
        self.fake.install_tmux_stub(panes=[pane(session_name="dev:server:1.2")])
        self.assertEqual(self.target_id("tmux:dev:server:1.2"), "%1")
        self.herdr(["one"], "w1:p9")
        self.fake.install_tmux_stub(panes=[pane(session_name="%99")])
        for address in ("tmux:%99", "herdr:w1:p9"):
            self.cli("send", "--dry-run", address, "hi", code=4)

    def test_candidate_addresses(self):
        self.herdr([str(i) for i in range(7)])
        self.fake.install_tmux_stub(panes=[pane(session_name="a/b/0")])
        hints = [f"  herdr:a/b/{i}  (herdr:w1:p{i})" for i in range(7)]
        for i in range(7):
            self.assertEqual(self.target_id(f"herdr:w1:p{i}"), f"w1:p{i}")
        self.assertEqual(self.target_id("tmux:%1"), "%1")
        for command in ("read", "send"):

            def check(target, code, expected):
                args = (
                    [command, target] if command == "read" else [command, "--dry-run", target, "hi"]
                )
                result = self.cli(*args, code=code)
                self.assertEqual(result.stderr.splitlines()[1:], expected)

            check("herdr:a/b", 5, ["candidates:", *hints])
            check("herdr:a/b/MISSING", 4, ["did you mean:", *hints[:5]])
            check("herdr:A/b/0", 4, [])
            check("a/b/0", 5, ["candidates:", hints[0], "  tmux:a/b/0:0.0  (tmux:%1)"])
            check("tmux:a/b/0:9.0", 4, ["did you mean:", "  tmux:a/b/0:0.0  (tmux:%1)"])
