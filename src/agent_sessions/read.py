"""Read an Orca mailbox or capture the tail of a terminal pane."""
import json
import re
import subprocess
import unicodedata
from datetime import datetime, timezone

from . import orca
from .model import Session, SourceError
from .render import cell
from .runner import Runner, output
from .target import TargetError, address


def record(source, target, kind, sender, body, timestamp, *, recipient=None,
           message_id=None, subject=None, thread_id=None):
    return {"source": source, "target": target, "id": message_id, "type": kind,
            "structured": source == "orca", "from": sender, "to": recipient,
            "subject": subject, "body": body, "timestamp": timestamp, "thread_id": thread_id}


def messages(name: str, limit: int, runner: Runner | None) -> list[dict]:
    query = orca.reader(runner)
    names = list(dict.fromkeys(bot["name"] for bot in query("bots", "bot", "list", "--json")
                               if orca.optional_text(bot.get("name"))))
    if name not in names:
        reason = f"target not found: orca:{cell(name)}"
        if names:
            reason += "\ndid you mean:\n" + "\n".join(f"  orca:{cell(n)}" for n in names)
        raise TargetError(4, reason, [])
    owners = {}
    for bot in names:
        for chat in query("chats", "bot", "chats", "--bot", bot, "--all", "--json"):
            if orca.optional_text(chat.get("id")):
                owners["orca_session_id:" + chat["id"]] = bot
    inbox = query("messages", "orchestration", "inbox", "--json", "--full", "--limit", "1000")
    runs = {}
    for msg in inbox:
        sender = orca.optional_text(msg.get("from_handle"))
        recipient = orca.optional_text(msg.get("to_handle"))
        run_id = orca.optional_text(msg.get("run_id"))
        if sender in owners and recipient and recipient.startswith("dispatch:") and run_id:
            runs["run:" + run_id] = owners[sender]
    owners.update(runs)

    def participant(handle):
        return "bot:" + owners[handle] if handle in owners else handle

    rows = []
    for msg in inbox:
        sender = orca.optional_text(msg.get("from_handle"))
        recipient = orca.optional_text(msg.get("to_handle"))
        if name not in (owners.get(sender), owners.get(recipient)):
            continue
        if not all(isinstance(msg.get(k), str) for k in ("id", "type", "body", "created_at")):
            raise SourceError("invalid Orca message")
        if any(msg.get(k) is not None and not isinstance(msg[k], str) for k in ("subject", "thread_id")):
            raise SourceError("invalid Orca message metadata")
        stamp = msg["created_at"]
        if not stamp.endswith("Z"):
            try:
                when = datetime.fromisoformat(stamp)
                if when.tzinfo is None:
                    raise ValueError
                stamp = when.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
            except (ValueError, OverflowError) as exc:
                raise SourceError("invalid Orca timestamp") from exc
        rows.append(record("orca", "orca:" + name, msg["type"], participant(sender),
                           msg["body"], stamp, recipient=participant(recipient),
                           message_id=msg["id"], subject=msg.get("subject"),
                           thread_id=msg.get("thread_id")))
        if len(rows) == limit:
            break
    return rows[::-1]


def capture(argv: list[str]) -> subprocess.CompletedProcess[str]:
    # text=True would translate embedded carriage returns in the raw JSON body.
    result = subprocess.run(argv, capture_output=True, timeout=5)
    return subprocess.CompletedProcess(argv, result.returncode, result.stdout.decode(),
                                       result.stderr.decode(errors="replace"))


def terminal(session: Session, limit: int, runner: Runner | None) -> dict:
    if session.source == "herdr":
        argv = ["herdr", "pane", "read", session.pane_id, "--source", "recent",
                "--lines", str(limit), "--format", "text"]
    else:
        argv = ["tmux", "capture-pane", "-p", "-J", "-t", session.pane_id, "-S", f"-{limit}"]
    lines = output(runner or capture, argv).rstrip("\n").split("\n")
    body = "\n".join(lines[-limit:])
    target = f"{session.source}:{address(session)}"
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    return record(session.source, target, "terminal-output", target, body, stamp)


# CSI, terminal strings (OSC/DCS/SOS/PM/APC), and ordinary ESC sequences.
_ESCAPE = re.compile(
    r"(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]"
    r"|(?:\x1b[]PX^_]|[\x90\x98\x9d-\x9f]).*?(?:\x07|\x1b\\|\x9c|\Z)"
    r"|\x1b[ -/]*[0-~]", re.DOTALL)


def clean(text: str) -> str:
    return "".join(c for c in _ESCAPE.sub("", text)
                   if c in "\n\t" or unicodedata.category(c) != "Cc")


def render(records: list[dict], fmt: str) -> str:
    if fmt == "json":
        return json.dumps(records, ensure_ascii=False, indent=2)
    if not records:
        return "No messages."
    lines = []
    for row in records:
        if not row["structured"] and not row["body"]:
            return "No output."
        route = cell(clean(row["from"] or "-"))
        if row["structured"]:
            route += " → " + cell(clean(row["to"] or "-"))
        header = f"── {cell(clean(row['timestamp']))}  {cell(clean(row['type']))}  {route}"
        if row["id"]:
            header += "  [" + cell(clean(row["id"])) + "]"
        lines.append(header)
        if row["subject"]:
            lines.append("   Subject: " + cell(clean(row["subject"])))
        lines.extend("   " + line for line in clean(row["body"]).split("\n"))
    return "\n".join(lines)
