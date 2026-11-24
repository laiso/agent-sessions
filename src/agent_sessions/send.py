import json
import unicodedata
import uuid

from . import observe, read
from .model import Session, SourceError, TmuxSession
from .render import cell
from .runner import Runner, output
from .target import address
from .tmux import DELIVERY_IDENTITY, validated_identity


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


def tmux_command(target: TmuxSession, data: str, buf: str) -> list[str]:
    """One tmux invocation that pastes data into exactly one pane.

    paste-buffer writes to the target pane only, even when synchronize-panes is on
    (send-keys would reach every synchronized pane). Without -r, a newline in the
    buffer is pasted as CR, which is the Enter key.
    """
    # if-shell -F evaluates in the same server that executes the paste. Only
    # numeric identities and a generated buffer name enter the command string.
    pid = target.pane_id
    expected = validated_identity(target)
    check = f"#{{==:{DELIVERY_IDENTITY}:#{{pane_dead}}:#{{pane_input_off}},{expected}:0:0}}"
    return ["tmux", "set-buffer", "-b", buf, "--", tmux_literal(data),
            ";", "if-shell", "-F", "-t", pid, check,
            f"paste-buffer -d -b {buf} -t {pid} ; display-message -p {buf}-pasted",
            f"display-message -p {buf}-refused"]


class PaneUnavailable(SourceError):
    """tmux checked the pane and did not paste: nothing was delivered."""


def tmux_paste(target: TmuxSession, data: str, runner: Runner) -> None:
    buf = f"agent-sessions-{uuid.uuid4().hex}"
    argv = tmux_command(target, data, buf)
    try:
        # Hooks can print their own lines on this client. Only the selected
        # branch's acknowledgement describes this particular paste.
        lines = output(runner, argv).splitlines()
        pasted = f"{buf}-pasted" in lines
        refused = f"{buf}-refused" in lines
        if refused and not pasted:
            raise PaneUnavailable("target pane changed, exited, or has input disabled")
        if not pasted or refused:
            raise SourceError("missing or conflicting tmux paste acknowledgement")
    except SourceError as exc:
        try:
            output(runner, ["tmux", "delete-buffer", "-b", buf])
        except SourceError as cleanup:
            if str(cleanup) not in (f"no buffer {buf}", f"unknown buffer: {buf}"):
                raise type(exc)(f"{exc}; temporary buffer {buf} may remain ({cleanup})") from exc
        raise


def send_input(target: Session, message: str, enter: bool, runner: Runner) -> None:
    pid = target.pane_id
    if target.source == "tmux":
        if not isinstance(target, TmuxSession):
            raise SourceError("missing tmux pane identity")
        try:
            tmux_paste(target, message, runner)
        except PaneUnavailable as exc:
            raise SourceError(f"nothing sent: {exc}") from exc
        except SourceError as exc:
            # A lost client acknowledgement can follow a successful paste.
            raise SourceError(f"text delivery not confirmed: {exc}") from exc
        if enter:
            try:
                tmux_paste(target, "\n", runner)
            except PaneUnavailable as exc:
                raise SourceError(f"text sent, Enter not sent: {exc}") from exc
            except SourceError as exc:
                raise SourceError(f"text sent, Enter delivery not confirmed: {exc}") from exc
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


def transmit(target: Session, message: str, enter: bool, runner: Runner) -> None:
    def screen():
        return read.clean(read.terminal_text(target, 100, runner, visible=True))

    try:
        before = screen()
    except SourceError as exc:
        raise SourceError(f"nothing sent: could not read initial pane: {exc}") from exc
    send_input(target, message, enter, runner)
    check = observe.submitted if enter else observe.staged
    if not observe.observed(lambda: check(before, screen(), message)):
        raise SourceError("pane did not confirm submission" if enter else "pane did not confirm staged text")


def result(target: Session, message: str, enter: bool, dry_run: bool, fmt: str) -> str:
    if fmt == "json":
        return json.dumps(dict(sent=not dry_run, dry_run=dry_run, enter=enter,
                               target=target.record(), message=message), ensure_ascii=False, indent=2)
    action = "Would send to" if dry_run else "Sent to"
    state = ", ".join(filter(None, (target.agent, target.status)))
    destination = target.session if target.source == "orca" else address(target)
    heading = cell(f"{action} {target.source} {destination} ({target.pane_id}) [{state}]")
    return f"{heading}\n> {message}" + (" ⏎" if enter else "")
