import argparse
import shutil
import sys

from . import herdr, send, tmux
from .model import Session, SourceError
from .render import render_json, render_table
from .runner import Runner, run
from .target import TargetError, resolve, split_target


def positive_width(value: str) -> int:
    width = int(value)
    if width < 1:
        raise argparse.ArgumentTypeError("width must be positive")
    return width


def collect_sessions(source: str, runner: Runner) -> tuple[list[Session], bool]:
    sessions = []
    readable = False
    for name, collect in (("herdr", herdr.collect), ("tmux", tmux.collect)):
        if source in ("all", name):
            try:
                sessions.extend(collect(runner))
                readable = True
            except SourceError as exc:
                reason = " ".join(str(exc).splitlines())
                print(f"agent-sessions: {name}: {reason}", file=sys.stderr)
    return sessions, readable


def send_main(argv: list[str], runner: Runner) -> int:
    parser = argparse.ArgumentParser(prog="agent-sessions send", allow_abbrev=False)
    parser.add_argument("--no-enter", action="store_true", help="send text without Enter")
    parser.add_argument("--dry-run", action="store_true", help="resolve the target and preview the message")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("target")
    parser.add_argument("message", nargs="+", help="use -- before a message starting with '-'")
    args = parser.parse_args(argv)
    try:
        message = send.message_text(args.message)
    except ValueError as exc:
        parser.error(str(exc))
    source, _ = split_target(args.target)
    sessions, readable = collect_sessions(source, runner)
    if not readable:
        return 3
    try:
        target = resolve(args.target, sessions)
    except TargetError as exc:
        print(f"agent-sessions: send: {exc}", file=sys.stderr)
        return exc.code
    enter = not args.no_enter
    if not args.dry_run:
        try:
            send.transmit(target, message, enter, runner)
        except SourceError as exc:
            reason = " ".join(str(exc).splitlines())
            print(f"agent-sessions: send: {reason}", file=sys.stderr)
            return 6
    print(send.result(target, message, enter, args.dry_run, args.format))
    return 0


def main(argv: list[str] | None = None, runner: Runner = run) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["send"]:
        return send_main(argv[1:], runner)
    if argv[:1] == ["list"]:
        argv = argv[1:]
    parser = argparse.ArgumentParser(prog="agent-sessions", description="List Herdr and tmux panes.",
                                     epilog="Use agent-sessions send --help to send a message.")
    parser.add_argument("--source", choices=("all", "herdr", "tmux"), default="all")
    parser.add_argument("--format", choices=("table", "json"), default="table")
    parser.add_argument("--agents-only", action="store_true")
    parser.add_argument("--no-header", action="store_true")
    parser.add_argument("--width", type=positive_width, metavar="N")
    args = parser.parse_args(argv)
    sessions, readable = collect_sessions(args.source, runner)
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
