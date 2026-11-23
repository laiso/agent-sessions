import json
import os
import unittest

import support  # Set up the src layout for unittest discovery.
from agent_sessions.model import Session
from agent_sessions.render import render_json, render_table
from agent_sessions.textwidth import display_width, truncate


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.row = Session("tmux", "開発", "1.2", "%2", None, "shell", "日本語" * 5,
                           os.path.expanduser("~/work/日本語日本語"), False)

    def test_fullwidth_truncation(self):
        self.assertEqual(display_width("aＡあ"), 5)
        self.assertEqual(truncate("あいうえお", 8), "あいう…")
        self.assertEqual(truncate("あいうえ", 8), "あいうえ")

    def test_width_priority_and_alignment(self):
        table = render_table([self.row])
        width = max(map(display_width, table.splitlines()))
        self.assertIn("~/work/日本語日本語", render_table([self.row], width - 10))
        heading, line = render_table([self.row], width - 10).splitlines()
        self.assertEqual(display_width(heading[:heading.index("CWD")]),
                         display_width(line[:line.index("~/")]))
        self.assertLessEqual(display_width(line), width - 10)
        small = render_table([self.row], 1, False)
        self.assertIn("日本語…", small)
        self.assertTrue(small.endswith("…"))
        self.assertIn("開発", small)

    def test_empty(self):
        self.assertEqual(render_table([], header=False), "No sessions found.")
        self.assertEqual(render_json([]), "[]")

    def test_json_values(self):
        self.row.command = None
        raw = render_json([self.row])
        self.assertIn("開発", raw)
        self.assertEqual(json.loads(raw)[0], vars(self.row))
