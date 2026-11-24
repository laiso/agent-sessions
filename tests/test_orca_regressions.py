import json
from tests.cli_cases import CliCase
from tests.helpers import write_executable


class DiscoveryTests(CliCase):
    def binary(self, path, tag, **options):
        body = self.fake.make_orca_script_body(tag=tag, bots=[dict(name=tag, id=tag)], **options)
        return write_executable(path, "#!" + str(self.fake.bin_dir / "python3") + "\n" + body)

    def install_system(self, system, extra=""):
        root = self.fake.root / "os-root"
        self.customize_host(
            f"""from pathlib import Path
import platform
platform.system=lambda:{system!r}
join=Path.__truediv__
def sandbox_root(path,other):
    return join(Path({str(root)!r}) if path==Path('/') else path,other)
Path.__truediv__=sandbox_root
"""
            + extra
        )
        return root

    def names(self):
        return [r["session"] for r in self.rows("--source", "orca")]

    def test_automatic_candidates(self):
        self.install_system("Linux")
        shim = self.fake.xdg_dir / "orca/linux-orca-cli-shim/orca"
        app = self.fake.home_dir / ".local/opt/orca/orca.AppImage"
        target = self.binary(self.fake.root / "shim-target", "shim")
        shim.parent.mkdir(parents=True)
        shim.symlink_to(target)
        self.binary(app, "app")
        self.assertEqual(self.names(), ["shim"])
        for mode in (0o600, 0o775, 0o757):
            target.chmod(mode)
            self.assertEqual(self.names(), ["app"])
        app.chmod(0o777)
        self.assertEqual(self.names(), [])
        target.chmod(0o700)
        for uid, expected in [(0, ["shim"]), (1001, ["shim"]), (1002, [])]:
            self.install_system(
                "Linux",
                f"""
import os,types
os.geteuid=lambda:1001
stat=Path.stat
def shim_stat(path,*args,**kwargs):
    info=stat(path,*args,**kwargs)
    if path==Path({str(shim)!r}):
        return types.SimpleNamespace(st_mode=info.st_mode,st_uid={uid})
    return info
Path.stat=shim_stat
""",
            )
            self.assertEqual(self.names(), expected)
        self.extra["ORCA_CLI_COMMAND"] = str(app)
        app.chmod(0o700)
        self.assertEqual(self.names(), ["app"])
        self.extra["AGENT_SESSIONS_ORCA"] = str(self.fake.root / "absent")
        self.assertEqual(self.names(), [])

    def test_decoy_never_runs(self):
        self.install_system("Linux")
        for name in ("orca", "orca.AppImage"):
            self.fake.install_script(
                name, f"from pathlib import Path\nPath({str(self.fake.root / 'invoked')!r}).touch()"
            )
        self.assertEqual(self.names(), [])
        self.assertFalse((self.fake.root / "invoked").exists())

    def test_platform_installation_order(self):
        root = self.install_system("Darwin")
        home = self.fake.home_dir
        paths = [
            home / ".local/bin/orca",
            *(
                base / bundle
                for bundle in (
                    "Applications/Orca.app/Contents/Resources/bin/orca",
                    "Applications/Orca Desktop.app/Contents/MacOS/orca",
                )
                for base in (root, home)
            ),
        ]
        for i, path in enumerate(paths):
            self.binary(path, f"app{i}")
        for i, path in enumerate(paths):
            self.assertEqual(self.names(), [f"app{i}"])
            path.chmod(0o777)
        self.install_system("Other")
        self.assertEqual(self.names(), [])
        paths[0].chmod(0o700)
        self.extra["ORCA_CLI_COMMAND"] = str(paths[0])
        self.assertEqual(self.names(), ["app0"])

    def test_uppercase_gui_excluded(self):
        root = self.install_system(
            "Darwin",
            """
import os
stat, access=Path.stat,os.access
def casefolded(path):
    p=Path(path)
    return p.with_name('Orca') if p.name=='orca' and p.parent.name=='MacOS' else path
Path.stat=lambda p,*a,**kw: stat(casefolded(p),*a,**kw)
os.access=lambda p,*a,**kw: access(casefolded(p),*a,**kw)
""",
        )
        for base in (root, self.fake.home_dir):
            for bundle in ("Orca.app", "Orca Desktop.app"):
                path = base / "Applications" / bundle / "Contents/MacOS/Orca"
                self.binary(path, "gui")
        self.assertEqual(self.names(), [])
        self.assertEqual(self.fake.orca_calls("gui"), [])

    def test_fallback_queries(self):
        for system in ("Linux", "Darwin"):
            root = self.install_system(system)
            if system == "Linux":
                shim = self.fake.xdg_dir / "orca/linux-orca-cli-shim/orca"
                app = self.fake.home_dir / ".local/opt/orca/orca.AppImage"
            else:
                shim = self.fake.home_dir / ".local/bin/orca"
                app = root / "Applications/Orca.app/Contents/Resources/bin/orca"
                self.binary(self.fake.home_dir / app.relative_to(root), "unused")
            for stage in ("bot_list", "worktree_ps"):
                tag = system + stage
                options = {stage + "_rc": 1, stage + "_stderr": "Orca CLI is unavailable"}
                self.binary(shim, tag, **options)
                self.binary(
                    app, tag + "app", worktree_ps_rc=1, worktree_ps_stderr="Orca CLI is unavailable"
                )
                self.assertEqual(self.names(), [tag + "app" if stage == "bot_list" else tag])
                initial = [["bot", "list", "--json"]]
                ps = [["worktree", "ps", "--json"]]
                self.assertEqual(
                    self.fake.orca_calls(tag), initial if stage == "bot_list" else initial + ps
                )
                self.assertEqual(
                    self.fake.orca_calls(tag + "app"), initial + ps if stage == "bot_list" else ps
                )
                self.assertEqual(self.fake.orca_calls("unused"), [])
            self.binary(shim, "explicit", bot_list_rc=1, bot_list_stderr="Orca CLI is unavailable")
            self.extra["AGENT_SESSIONS_ORCA"] = str(shim)
            self.assertEqual(self.names(), [])
            del self.extra["AGENT_SESSIONS_ORCA"]
            self.binary(shim, "broken", bot_list_rc=1, bot_list_stderr="other error")
            self.assertEqual(self.names(), [])

    def test_source_timeouts(self):
        log = self.fake.root / "timeouts"
        self.customize_host(f"""import subprocess
run=subprocess.run
def observed(argv,**kwargs):
    with open({str(log)!r},'a') as f: print(argv[1],kwargs.get('timeout'),file=f)
    return run(argv,**kwargs)
subprocess.run=observed
""")
        self.fake.install_tmux_stub()
        self.extra["AGENT_SESSIONS_ORCA"] = str(self.binary(self.fake.root / "orca", "alpha"))
        self.names()
        self.rows("--source", "tmux")
        self.assertEqual(log.read_text().splitlines(), ["bot 15", "worktree 15", "list-panes 5"])


class MailTests(CliCase):
    def configure(self, **kwargs):
        self.extra["AGENT_SESSIONS_ORCA"] = str(
            self.fake.install_script("orca", self.fake.make_orca_script_body(**kwargs))
        )

    def test_bot_home_and_state_priority(self):
        states = [
            (["working", "blocked"], "working"),
            (["idle", "blocked"], "blocked"),
            (["done", "waiting"], "blocked"),
            (["permission"], "blocked"),
            (["done"], "idle"),
            ([], "stopped"),
            (["other"], "unknown"),
            (["working"], "paused"),
        ]
        bots = [
            dict(id=str(i), name=f"bot{i}", paused=state == "paused")
            for i, (_, state) in enumerate(states)
        ]
        homes = [
            dict(bot=b["name"], botHome=True, path="/home", agents=[dict(state=s) for s in group])
            for b, (group, _) in zip(bots, states)
        ]
        homes.append(dict(bot="bot0", botHome=False, path="/wrong", agents=[]))
        self.configure(bots=list(reversed(bots)), worktrees=homes)
        rows = self.rows("--source", "orca")
        self.assertEqual(
            [(r["session"], r["status"], r["cwd"], r["agent"]) for r in rows],
            [(b["name"], state, "/home", None) for b, (_, state) in zip(bots, states)],
        )

    def test_bot_envelopes_and_empty(self):
        for system in ("Darwin", "Linux"):
            self.customize_host(f"import platform\nplatform.system=lambda:{system!r}")
            self.configure(tag=system)
            self.assertEqual(self.rows("--source", "all"), [])
            self.assertEqual(self.fake.orca_calls(system), [["bot", "list", "--json"]])
        for raw in (
            "{",
            "null",
            '{"result":{"bots":[]}}',
            '{"ok":1,"result":{"bots":[]}}',
            '{"ok":false}',
            '{"ok":true,"result":{"bots":{}}}',
        ):
            self.extra["AGENT_SESSIONS_ORCA"] = str(
                self.fake.install_script("orca", f"print({raw!r})")
            )
            self.assertEqual(self.cli("--source", "orca").stderr, "")
            self.cli("--source", "all", code=3)
            self.cli("read", "orca:a", code=3)
        self.configure(bots=[dict(name="bot", id="synthetic")], worktrees=[])
        (row,) = self.rows("--source", "orca")
        self.assertEqual((row["agent"], row["cwd"], row["status"]), (None, None, "unknown"))
        for bad in ("{}", "not JSON", '{"ok":false}', '{"result":{"worktrees":null}}'):
            bots = dict(
                ok=True,
                result=dict(bots=[dict(name="a", id="1"), dict(name="b", id="2", paused=True)]),
            )
            self.fake.install_script(
                "orca",
                f"import sys\nprint({json.dumps(bots)!r} if sys.argv[1]=='bot' else {bad!r})",
            )
            self.assertEqual(
                [(r["status"], r["cwd"]) for r in self.rows("--source", "orca")],
                [("unknown", None), ("paused", None)],
            )

    def test_mail_order_queries_and_time(self):
        a, b = "orca_session_id:alpha", "orca_session_id:beta"

        def msg(i, sender, recipient, run="r"):
            return dict(
                id=f"msg_{i}",
                type="status",
                body="line\n次",
                created_at="2026-01-01T02:00:05+02:00",
                from_handle=sender,
                to_handle=recipient,
                run_id=run,
            )

        inbox = [
            msg(5, "term_example", "run:r"),
            msg(4, "run:s", a),
            msg(3, b, "dispatch:y", "s"),
            msg(2, a, "dispatch:x"),
        ] + [msg(i, b, a) for i in range(30)]

        def configure():
            self.configure(
                bots=[{"name": "a"}, {"name": "b"}],
                chats_by_bot={"a": [{"id": "alpha"}], "b": [{"id": "beta"}]},
                inbox_messages=inbox,
            )

        configure()
        records = self.rows("read", "--limit", "3", "orca:a")
        self.assertEqual(
            [(r["from"], r["to"]) for r in records],
            [("bot:a", "dispatch:x"), ("bot:b", "bot:a"), ("term_example", "bot:a")],
        )
        self.assertEqual(
            (records[-1]["timestamp"], records[-1]["body"]), ("2026-01-01T00:00:05Z", "line\n次")
        )
        self.assertEqual(
            self.fake.orca_calls(),
            [
                ["bot", "list", "--json"],
                *[["bot", "chats", "--bot", n, "--all", "--json"] for n in ("a", "b")],
                ["orchestration", "inbox", "--json", "--full", "--limit", "1000"],
            ],
        )
        self.assertEqual(len(self.rows("read", "orca:a")), 20)
        inbox[0]["created_at"] = "invalidZ"
        configure()
        self.cli("read", "orca:a", code=3)
        self.configure(bots=[{"name": "a"}], inbox_rc=1)
        self.cli("read", "orca:a", code=3)
        self.configure(bots=[{"name": "a"}])
        self.assertEqual(self.cli("read", "orca:a").stdout, "No messages.\n")
        path = self.fake.bin_dir / "orca"
        self.intercept(path, "if argv[:2] == ['bot','chats']: sys.exit(1)")
        self.cli("read", "orca:a", code=3)
        self.assertIn("orca:a", self.cli("read", "orca:missing", code=4).stderr)
