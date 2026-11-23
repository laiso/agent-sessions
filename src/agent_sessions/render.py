import json
import os

from .model import Session
from .textwidth import display_width, truncate

HEADINGS = ["SOURCE", "SESSION", "PANE", "AGENT", "STATUS", "COMMAND", "CWD"]


def render_json(sessions: list[Session]) -> str:
    return json.dumps([session.record() for session in sessions], indent=2, ensure_ascii=False)


def short_path(path: str | None) -> str:
    home = os.path.expanduser("~")
    if path == home:
        return "~"
    if path and path.startswith(home + "/"):
        return "~" + path[len(home):]
    return path or "-"


def cell(value: str) -> str:
    return "".join(char if char.isprintable() else " " for char in value)


def render_table(sessions: list[Session], width: int | None = None, header: bool = True) -> str:
    if not sessions:
        return "No sessions found."
    rows = [HEADINGS] if header else []
    for s in sessions:
        rows.append(list(map(cell, [s.source, s.session, s.pane, s.agent or "-", s.status,
                                    s.command or "-", short_path(s.cwd)])))
    widths = [max(map(display_width, column)) for column in zip(*rows)]
    if width is not None:
        excess = max(0, sum(widths) + 2 * (len(widths) - 1) - width)
        for col in (5, 6):
            reduction = min(excess, max(0, widths[col] - 8))
            widths[col] -= reduction
            excess -= reduction

    lines = []
    for row in rows:
        cells = []
        for value, size in zip(row, widths):
            value = truncate(value, size)
            cells.append(value + " " * (size - display_width(value)))
        lines.append("  ".join(cells).rstrip())
    return "\n".join(lines)
