import re

from .model import Session, TmuxSession
from .render import cell


class TargetError(Exception):
    def __init__(self, code: int, reason: str, candidates: list[Session]):
        heading = "candidates:" if code == 5 else "did you mean:"
        lines = [reason, *([heading] if candidates else []),
                 *(cell(f"  {s.source}:{address(s)}  ({s.source}:{s.pane_id})") for s in candidates)]
        super().__init__("\n".join(lines))
        self.code = code


def split_target(target: str, *, reading: bool = False) -> tuple[str, str]:
    prefix, sep, rest = target.partition(":")
    if sep and (prefix in ("herdr", "tmux") or (reading and prefix == "orca")):
        return prefix, rest
    return "all", target


def address(s: Session) -> str:
    if s.source == "herdr":
        return f"{s.session}/{s.pane}"
    return f"{s.session}:{s.pane.partition(' ')[0]}"


def shared_tmux_view(row: Session) -> tuple | None:
    """Fields a physical pane shares across session-specific window numbers and focus."""
    if not isinstance(row, TmuxSession) or not re.fullmatch(r"%[0-9]+", row.pane_id):
        return None
    if not re.fullmatch(r"[1-9][0-9]*:[1-9][0-9]*:[1-9][0-9]*", row.delivery_identity):
        return None
    return (row.pane_id, row.delivery_identity, row.pane.partition(".")[2],
            row.status, row.agent, row.command, row.cwd)


def source_matches(source: str, value: str, explicit: bool, rows: list[Session]) -> list[Session]:
    if explicit:
        by_id = [s for s in rows if value == s.pane_id]
        id_shape = r"%[0-9]+" if source == "tmux" else r"w[0-9a-zA-Z]+:p[0-9a-zA-Z]+"
        if source == "tmux" and len(by_id) > 1:
            view = shared_tmux_view(by_id[0])
            if view is None or any(shared_tmux_view(row) != view for row in by_id[1:]):
                # Check before filtering dead panes: conflicting live/dead rows cannot
                # establish that the pane shown in a preview is the delivery target.
                raise TargetError(5, f"ambiguous target: tmux:{cell(value)}", by_id)
            return by_id[:1]
        if by_id or re.fullmatch(id_shape, value):
            return by_id

    separator = "/" if source == "herdr" else ":"
    session, sep, _ = value.rpartition(separator)
    named = [s for s in rows if sep and s.session == session and address(s) == value]
    if named:
        return named
    if source == "herdr":
        # A pane label itself may contain a slash.
        named = [s for s in rows if address(s) == value]
        if named or not explicit:
            return named
        return [s for s in rows if s.session == value]
    return [s for s in rows if s.session == value and isinstance(s, TmuxSession) and s.active]


def resolve(target: str, sessions: list[Session], *, allow_dead: bool = False) -> Session:
    prefix, value = split_target(target)
    matches = []
    near = []
    for source, separator in (("herdr", "/"), ("tmux", ":")):
        if prefix not in (source, "all"):
            continue
        rows = [s for s in sessions if s.source == source]
        matches.extend(source_matches(source, value, prefix != "all", rows))
        session, _, _ = value.rpartition(separator)
        near.extend(s for s in rows if s.session in (session, value))

    eligible = matches if allow_dead else [s for s in matches if not (s.source == "tmux" and s.status == "dead")]
    label = cell(target)
    if len(eligible) > 1:
        raise TargetError(5, f"ambiguous target: {label}", eligible)
    if eligible:
        return eligible[0]
    reason = f"target not found: {label}"
    if matches:
        reason += " (dead)"
    # Only suggest alternatives that the current operation can select.
    hints = [s for s in near if s not in matches
             and (allow_dead or not (s.source == "tmux" and s.status == "dead"))]
    raise TargetError(4, reason, hints[:5])
