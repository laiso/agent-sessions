import json
import os
import unicodedata

from .model import Session, SourceError
from .render import cell
from .runner import Runner, output
from .target import address


def message_text(words: list[str]) -> str:
    text = " ".join(words)
    if not text.strip():
        raise ValueError("message is empty")
    if len(text) > 4096:
        raise ValueError("message exceeds 4096 characters")
    if "\t" in text:
        # A tab reaches the pane as the Tab key (shell completion), not as literal text.
        raise ValueError("message contains a tab; use spaces instead")
    if any(unicodedata.category(ch) in ("Cc", "Cs", "Zl", "Zp") for ch in text):
        raise ValueError("message contains a control character or line break")
    return text


def tmux_literal(message: str) -> str:
    """tmux treats an argument ending in ';' as a command separator; '\\;' keeps it literal."""
    return message[:-1] + "\\;" if message.endswith(";") else message


def tmux_command(pid: str, message: str, enter: bool) -> list[str]:
    """One tmux invocation that pastes the text (and a newline for Enter) into one pane.

    paste-buffer writes to the target pane only, even when synchronize-panes is on,
    unlike send-keys which reaches every synchronized pane. Sending everything as one
    command list leaves no window between a check and the delivery.
    """
    buf = f"agent-sessions-{os.getpid()}"
    argv = ["tmux", "set-buffer", "-b", buf, "--", tmux_literal(message),
            ";", "paste-buffer", "-d", "-b", buf, "-t", pid]
    if enter:
        # Without -r, paste-buffer turns the newline into CR: the Enter key.
        argv += [";", "set-buffer", "-b", buf, "--", "\n",
                 ";", "paste-buffer", "-d", "-b", buf, "-t", pid]
    return argv


def transmit(target: Session, message: str, enter: bool, runner: Runner) -> None:
    pid = target.pane_id
    if target.source == "tmux":
        argv = tmux_command(pid, message, enter)
        try:
            output(runner, argv)
        except SourceError as exc:
            try:  # drop a buffer left behind by a failed paste
                runner(["tmux", "delete-buffer", "-b", argv[3]])
            except Exception:
                pass
            raise SourceError(f"tmux delivery failed (text may be partly sent): {exc}") from exc
        return

    text_cmd = ["herdr", "pane", "send-text", pid]
    # Herdr removes its global session/remote switches even after the pane ID.
    # Its send-text handler would send an inserted "--" literally.
    if message.partition("=")[0] in ("--session", "--remote", "--remote-keybindings"):
        output(runner, [*text_cmd, "-"])
        try:
            output(runner, [*text_cmd, message[1:]])
        except SourceError as exc:
            raise SourceError(f"leading '-' sent, remaining text failed: {exc}") from exc
    else:
        output(runner, [*text_cmd, message])
    if enter:
        try:
            output(runner, ["herdr", "pane", "send-keys", pid, "enter"])
        except SourceError as exc:
            raise SourceError(f"text sent, Enter failed: {exc}") from exc


def result(target: Session, message: str, enter: bool, dry_run: bool, fmt: str) -> str:
    if fmt == "json":
        return json.dumps(dict(sent=not dry_run, dry_run=dry_run, enter=enter,
                               target=target.record(), message=message), ensure_ascii=False, indent=2)
    action = "Would send to" if dry_run else "Sent to"
    state = ", ".join(filter(None, (target.agent, target.status)))
    heading = cell(f"{action} {target.source} {address(target)} ({target.pane_id}) [{state}]")
    return f"{heading}\n> {message}" + (" ⏎" if enter else "")
