import json
import socket
import unicodedata

from tests.cli_cases import CliCase, pane
from tests.helpers import sample_herdr_process_info, sample_herdr_snapshot


class CollectionTests(CliCase):
    def test_partial_sources_and_errors(self):
        absent = self.cli("--format", "json", code=3)
        self.assertEqual(json.loads(absent.stdout), [])
        self.assertEqual(
            [line.split(": ", 2)[1] for line in absent.stderr.splitlines()], ["herdr", "tmux"]
        )
        self.fake.install_tmux_stub(list_rc=1, list_stderr="no server running")
        for out, err, expected in [
            ("", "error\ndetails", "error details"),
            ('{"error":1}', "", '{"error":1}'),
            ('{"error":{"message":"offline"}}', "warning", "offline"),
            ("", "plain", "plain"),
        ]:
            self.fake.install_script(
                "herdr", f"import sys\nprint({out!r}); print({err!r},file=sys.stderr); sys.exit(1)"
            )
            result = self.cli("--no-header")
            self.assertEqual(result.stdout, "No sessions found.\n")
            self.assertEqual(result.stderr, f"agent-sessions: herdr: {expected}\n")
        self.fake.install_tmux_stub(panes=[pane()])
        self.assertIn(
            "agent-sessions: herdr: plain", self.cli("send", "--dry-run", "demo", "hi").stderr
        )
        self.cli("send", "herdr:missing", "hi", code=3)
        self.cli("send", "tmux:missing", "hi", code=4)

    def test_snapshot_mapping_and_partial_info(self):
        snap = sample_herdr_snapshot()
        data = snap["result"]["snapshot"]
        first, last = data["panes"]
        first.update(label="shared", display_agent="TEST", foreground_cwd="/foreground")
        middle = dict(first, pane_id="w1:p2", agent_status="blocked")
        last.update(label="shared", agent_status="done")
        data["panes"] = [last, middle, first]
        self.fake.install_herdr_stub(snapshot=snap, process_info=sample_herdr_process_info())
        a, b, c = self.rows("--source", "herdr")
        self.assertEqual(
            (a["pane"], a["agent"], a["cwd"], a["focused"]), ("shared", "TEST", "/foreground", True)
        )
        self.assertIsNotNone(a["command"])
        self.assertEqual((b["pane"], b["command"], b["status"]), ("shared#w1:p2", None, "blocked"))
        self.assertEqual((c["pane"], c["status"]), ("shared", "done"))

    def test_minimal_snapshot(self):
        self.fake.install_herdr_stub(
            snapshot={"result": {"snapshot": {"panes": [{"pane_id": "p", "workspace_id": "w"}]}}}
        )
        (row,) = self.rows("--source", "herdr")
        self.assertEqual(
            [row[k] for k in ("session", "pane", "agent", "status", "cwd", "focused")],
            ["w", "p", None, "unknown", None, False],
        )

    def test_invalid_snapshot_and_info(self):
        for raw in ("{", "null", "{}", '{"result":{"snapshot":{"panes":[{}]}}}'):
            self.fake.install_script("herdr", f"print({raw!r})")
            self.cli("--source", "herdr", code=3)
        snap = json.dumps(sample_herdr_snapshot())
        for raw in ("{", "null", '{"result":{"process_info":{"foreground_processes":[]}}}'):
            self.fake.install_script(
                "herdr", f"import sys\nprint({snap!r} if sys.argv[1]=='api' else {raw!r})"
            )
            self.assertTrue(all(r["command"] is None for r in self.rows("--source", "herdr")))

    def test_unicode_width(self):
        path = str(self.fake.home_dir / "work/日本語日本語")
        self.fake.install_tmux_stub(
            panes=[
                pane(session_name="開発", pane_current_command="日本語" * 5, pane_current_path=path)
            ]
        )

        def width(s):
            return sum(2 if unicodedata.east_asian_width(c) in ("F", "W") else 1 for c in s)

        full = self.cli("--source", "tmux").stdout
        limit = max(map(width, full.splitlines())) - 10
        header, line = self.cli("--source", "tmux", "--width", str(limit)).stdout.splitlines()
        self.assertIn("~/work/日本語日本語", line)
        self.assertEqual(width(header[: header.index("CWD")]), width(line[: line.index("~/")]))
        self.assertLessEqual(width(line), limit)
        tiny = self.cli("--source", "tmux", "--width", "1", "--no-header").stdout.rstrip()
        self.assertIn("日本語…", tiny)
        self.assertTrue(tiny.endswith("…"))
        self.assertIn("開発", self.cli("--source", "tmux", "--format", "json").stdout)

    def test_tmux_list_errors(self):
        for msg in ("no server running", "error connecting"):
            self.fake.install_tmux_stub(list_rc=1, list_stderr=msg)
            self.assertEqual(self.rows("--source", "tmux"), [])
        self.fake.install_script("tmux", "print('malformed')")
        self.cli("--source", "tmux", code=3)

    def test_pane_titles_and_order(self):
        host = socket.gethostname()
        hidden = [
            host,
            f"user@{host}",
            f"user@{host}:/tmp",
            f"user@{host} ~",
            f"user@{host} /tmp/project files",
        ]
        visible = [f"user@{host} release", f"user@{host} deploy review", f"user@{host}-other:/x"]
        self.fake.install_tmux_stub(
            panes=[
                pane(window_index=i, pane_title=t, pane_current_command="/bin/zsh")
                for i, t in reversed(list(enumerate(hidden + visible, start=7)))
            ]
        )
        rows = self.rows("--source", "tmux")
        self.assertEqual(
            [r["pane"] for r in rows],
            [
                f"{i}.0" + (f" {t}" if t in visible else "")
                for i, t in enumerate(hidden + visible, start=7)
            ],
        )
        self.assertTrue(all(r["status"] == "shell" and not r["focused"] for r in rows))
        self.fake.install_tmux_stub(
            panes=[
                pane(window_index=w, pane_index=i, pane_current_command="my-codex")
                for w, i in [(10, 0), (2, 12), (2, 3)]
            ]
        )
        rows = self.rows("--source", "tmux")
        self.assertEqual([r["pane"] for r in rows], ["2.3", "2.12", "10.0"])
        self.assertTrue(all(r["agent"] is None and r["status"] == "running" for r in rows))

    def test_pane_process_arguments(self):
        proc = self.fake.root / "proc"
        self.customize_host(f"""import pathlib,platform
platform.system=lambda:'Linux'
join=pathlib.Path.__truediv__
def redirected(path,part):
    return join(pathlib.Path({str(proc)!r}) if str(path)=='/proc' else path,part)
pathlib.Path.__truediv__=redirected
""")
        cases = [
            (["node", "/dir with spaces/claude", "--resume"], "claude"),
            (["vim", "claude"], None),
            (["npx", "-y", "codex"], "codex"),
            (["/usr/bin/gemini", "--yolo"], "gemini"),
            (["node", "--require", "/x/claude", "/x/app.js"], None),
            (["node", "--require", "/x/hook", "/x/claude"], "claude"),
            (["node", "--require=/x/hook", "/x/codex"], "codex"),
            (["node", "-e", "claude"], None),
            (["python3", "-m", "aider"], "aider"),
            (["python3", "-W", "ignore", "/x/aider"], "aider"),
            (["python3", "-c", "claude"], None),
            (["npx", "-p", "claude", "other"], None),
            (["uvx", "--from", "x", "aider"], "aider"),
            (["uv", "run", "aider"], "aider"),
            (["node", "--eval=x", "claude"], None),
            (["python3", "-cimport time", "codex"], None),
            (["python3", "-maider"], "aider"),
            (["deno", "run", "-c", "deno.json", "/x/claude"], "claude"),
            (["bun", "-c", "bunfig.toml", "/x/claude"], "claude"),
            (["npx", "--workspace", "claude", "other"], None),
            (["uv", "run", "--with-requirements", "requirements.txt", "aider"], "aider"),
            (["uv", "run", "tool", "claude"], None),
        ]
        panes = []
        for idx, (argv, _) in enumerate(cases):
            pid = str(idx + 8000000)
            panes.append(
                pane(
                    pane_pid=pid,
                    pane_index=idx,
                    pane_title="作業",
                    session_attached=int(idx != 2),
                    pane_dead=int(idx == 2),
                )
            )
            task = proc / pid / "task" / pid
            task.mkdir(parents=True)
            (task / "children").write_text("")
            (proc / pid / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
        self.fake.install_tmux_stub(panes=panes)
        rows = self.rows("--source", "tmux")
        self.assertEqual(
            [(r["command"], r["agent"]) for r in rows],
            [(" ".join(argv), agent) for argv, agent in cases],
        )
        self.assertEqual(
            (rows[0]["pane"], rows[0]["focused"], rows[2]["status"]), ("0.0 作業", True, "dead")
        )

    def test_ps_snapshot_and_fallback(self):
        self.fake.install_tmux_stub(panes=[pane(pane_pid="123"), pane(pane_pid="55", pane_index=1)])
        self.customize_host("import platform\nplatform.system=lambda:'Darwin'")
        for raw, expected in [
            (
                "123 1 sh\n1010 123 node   /bin/claude --resume\n99 123 vim\n2010 1010 git\n55 1 sleep 300",
                ["node   /bin/claude --resume", "sleep 300"],
            ),
            ("123 1 codex\nbad", ["sh", "sh"]),
            ("33 1 unrelated", ["sh", "sh"]),
        ]:
            self.fake.install_script("ps", f"print({raw!r})")
            self.assertEqual([r["command"] for r in self.rows("--source", "tmux")], expected)
        self.fake.install_script("ps", "raise SystemExit(1)")
        self.assertEqual(self.rows("--source", "tmux")[0]["status"], "shell")
        self.customize_host("import platform\nplatform.system=lambda:'Other'")
        self.fake.install_script("ps", "print('123 1 codex')")
        self.assertEqual(self.rows("--source", "tmux")[0]["command"], "sh")

    def test_capture_contract(self):
        tool = self.fake.install_tmux_stub(panes=[pane()], captures={"%1": (0, "\n\n", "")})
        self.assertEqual(self.cli("read", "tmux:%1").stdout, "No output.\n")
        self.assertEqual(
            self.fake.tmux_calls()[-1], ["capture-pane", "-p", "-J", "-t", "%1", "-S", "-50"]
        )
        raw = (
            b"old\n\n\xff"
            + "日本\u2067a\u2069\u202eb\u202c👩\u200d💻".encode()
            + b"\x1b]8;;url\x07link\x1b]8;;\x07\n\n"
        )
        self.intercept(
            tool, f"if argv[0]=='capture-pane':\n    sys.stdout.buffer.write({raw!r}); sys.exit(0)"
        )
        body = self.rows("read", "--limit", "2", "tmux:%1")[0]["body"]
        self.assertEqual(body, "\n" + raw.decode(errors="replace").split("\n")[2])
        self.assertTrue(
            self.cli("read", "--limit", "2", "tmux:%1").stdout.endswith(
                "   \n   �日本ab👩\u200d💻link\n"
            )
        )
        self.fake.install_herdr_stub(
            snapshot=sample_herdr_snapshot(), pane_read={"w1:p1": (0, "ok", "")}
        )
        self.cli("read", "--limit", "2", "herdr:w1:p1")
        self.assertEqual(
            self.fake.herdr_calls()[-1],
            ["pane", "read", "w1:p1", "--source", "recent", "--lines", "2", "--format", "text"],
        )
