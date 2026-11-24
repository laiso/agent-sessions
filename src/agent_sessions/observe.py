"""Bounded, read-only checks of delivery evidence and visible terminal prompts."""
import re
import time

from . import read
from .model import SourceError


# Agents can take a second or more to redraw after Enter, and a busy Orca can
# record a message late. A short window reports real deliveries as unconfirmed,
# which invites a duplicate resend. Poll for about four seconds in total.
DELAYS = (0, .1, .25, .5, .75, 1, 1.25)


def observed(check):
    deadline = time.monotonic() + 4
    last_error = None
    for delay in DELAYS:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        if delay:
            time.sleep(min(delay, remaining))
        try:
            if check():
                return True
            last_error = None
        except SourceError as exc:
            # Only observations are retried. A slow read uses up the poll budget.
            last_error = exc
    if last_error is not None:
        raise last_error
    return False


# Match a prompt at the start of a row, never a marker embedded in prose.
PROMPT = re.compile(r"^[ \t│┃]*[›❯>](?:\s|$)")
BUSY = re.compile(r"esc to (?:interrupt|cancel)|ctrl[+-]c to interrupt|^[✶✻✽✳✢].*…|^[⠁-⣿]+\s+\S", re.I | re.M)
COMPLETE = re.compile(r"^\s*[─✻✶✽✳*]*\s*(?:Worked|Cooked|Brewed|Churned|Baked|Sautéed) for \d", re.M)


def prompt_index(lines):
    # The composer can be followed by separators and a few status/footer rows.
    return next((i for i in range(len(lines) - 1, max(-1, len(lines) - 6), -1)
                 if PROMPT.match(lines[i])), None)


def agent_status(screen, focused, title):
    lines = screen.rstrip().splitlines()
    index = prompt_index(lines)
    tail = "\n".join(line for line in lines[-10:] if not PROMPT.match(line))
    if re.search(r"enter to (?:confirm|select)|waiting for (?:user confirmation|permission)", tail, re.I):
        return "blocked"
    if BUSY.search(tail) or re.match(r"^[⠁-⣿◐-◓] ", title):
        return "running"
    if index is None and "? for shortcuts" not in "\n".join(lines[-2:]).lower():
        return "running"
    reply = re.compile(r"^\s*[•⏺●]\s+\S")
    if index is not None and any(reply.match(line) for line in lines[index + 1:]):
        # The last marker belongs to the transcript; a new composer is not visible yet.
        return "running"
    history = lines[:index]
    previous = next((i for i in reversed(range(len(history))) if PROMPT.match(history[i])), None)
    turn = history if previous is None else history[previous + 1:]
    answered = previous is not None and any(reply.match(line) for line in turn)
    if not focused and (answered or COMPLETE.search("\n".join(turn))):
        return "done"
    return "idle"


def update_tmux_status(sessions, runner):
    panes = {}
    for session in sessions:
        if session.source == "tmux" and session.agent and session.status == "running":
            panes.setdefault((session.pane_id, session.delivery_identity), []).append(session)
    for views in panes.values():
        pane = views[0]
        try:
            screen = read.clean(read.terminal_text(pane, 50, runner, visible=True))
        except read.SourceError:
            continue
        focused = any(view.focused for view in views)
        status = agent_status(screen, focused, pane.pane.partition(" ")[2])
        for view in views:
            view.status = status


def staged(before, after, message):
    if after == before:
        return False
    lines = after.rstrip().splitlines()
    index = prompt_index(lines)
    line = lines[index] if index is not None else (lines[-1] if lines else "")
    old = before.rstrip().splitlines()
    old_index = prompt_index(old)
    previous = old[old_index] if old_index is not None else (old[-1] if old else "")
    if line == previous or (index is None) != (old_index is None):
        return False
    if index is not None:
        marker = PROMPT.match(line).group().strip()
        if marker != PROMPT.match(previous).group().strip():
            return False
        line = PROMPT.sub("", line, count=1)
        previous = PROMPT.sub("", previous, count=1)
    text = message.rstrip()
    return line.rstrip() in (text, previous + text, previous.rstrip() + " " + text)


def submitted(before, after, message):
    if before == after:
        return False
    old = before.rstrip().splitlines()
    lines = after.rstrip().splitlines()
    index = prompt_index(lines)
    command = message.strip()
    if index is not None:
        # A changing footer below a populated composer is not a submission.
        draft = PROMPT.sub("", lines[index], count=1).strip()
        old_index = prompt_index(old)
        if draft and (old_index is None or lines[index] != old[old_index] or command in draft):
            return False

        expected = {command}
        if old_index is not None:
            prior_draft = PROMPT.sub("", old[old_index], count=1)
            expected.add((prior_draft + message).strip())

        def entries(rows):
            return sum(bool(PROMPT.match(line)) and PROMPT.sub("", line, count=1).strip() in expected
                       for line in rows)
        return entries(lines[:index]) > entries(old)
    # Ordinary shells return to their prompt after executing a line. Programs
    # such as cat echo a second copy after the terminal driver submits the line.
    if old and old[-1].strip().endswith(("$", "#", "%")) and lines and lines[-1] == old[-1]:
        echoed = old[-1].rstrip() + " " + message
        return lines[:-1].count(echoed) > old.count(echoed)
    return (len(lines) >= 2 and lines[-1] == lines[-2]
            and lines[-1] in (message.rstrip(), (old[-1] if old else "") + message.rstrip())
            and after.count(message.rstrip()) >= before.count(message.rstrip()) + 2)
