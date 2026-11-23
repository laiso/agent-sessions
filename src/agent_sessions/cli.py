import argparse
import shutil
import sys

from . import herdr, tmux
from .model import SourceError
from .render import render_json, render_table
from .runner import Runner, run


def positive_width(value: str) -> int:
    width = int(value)
    if width < 1:
        raise argparse.ArgumentTypeError("width must be positive")
    return width


def main(argv: list[str] | None = None, runner: Runner = run) -> int:
    parser = argparse.ArgumentParser(prog="agent-sessions", description="List Herdr and tmux panes.")
    parser.add_argument("--source", choices=("all", "herdr", "tmux"), default="all")
    parser.add_argument("--format", choices=("table", "json"), default="table")
    parser.add_argument("--agents-only", action="store_true")
    parser.add_argument("--no-header", action="store_true")
    parser.add_argument("--width", type=positive_width, metavar="N")
    args = parser.parse_args(argv)
    sessions = []
    readable = False
    for name, collect in (("herdr", herdr.collect), ("tmux", tmux.collect)):
        if args.source in ("all", name):
            try:
                sessions.extend(collect(runner))
                readable = True
            except SourceError as exc:
                reason = " ".join(str(exc).splitlines())
                print(f"agent-sessions: {name}: {reason}", file=sys.stderr)
    if args.agents_only:
        sessions = [s for s in sessions if s.agent]
    if args.format == "json":
        print(render_json(sessions))
    else:
        width = args.width
        if width is None and sys.stdout.isatty():
            width = shutil.get_terminal_size().columns
        print(render_table(sessions, width, not args.no_header))
    return 0 if readable else 3
