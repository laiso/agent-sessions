from dataclasses import dataclass


class SourceError(Exception):
    """The requested source could not be read."""


@dataclass
class Session:
    source: str
    session: str
    pane: str
    pane_id: str
    agent: str | None
    status: str
    command: str | None
    cwd: str | None
    focused: bool
