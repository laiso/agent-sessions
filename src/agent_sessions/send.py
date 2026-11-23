import json
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


def transmit(target: Session, message: str, enter: bool, runner: Runner) -> None:
    pid = target.pane_id
    if target.source == "herdr":
        text_cmd = ["herdr", "pane", "send-text", pid]
        enter_cmd = ["herdr", "pane", "send-keys", pid, "enter"]
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
    else:
        # With synchronize-panes on, send-keys reaches every pane in the window.
        # Refuse instead of changing the user's setting.
        synced = output(runner, ["tmux", "display-message", "-p", "-t", pid, "#{pane_synchronized}"])
        if synced.strip() == "1":
            raise SourceError("synchronize-panes is on for this window; "
                              "the message would reach every pane, so nothing was sent")
        output(runner, ["tmux", "send-keys", "-t", pid, "-l", "--", tmux_literal(message)])
        enter_cmd = ["tmux", "send-keys", "-t", pid, "Enter"]
    if enter:
        try:
            output(runner, enter_cmd)
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
