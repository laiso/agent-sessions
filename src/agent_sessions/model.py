from dataclasses import dataclass, fields


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

    def record(self) -> dict:
        """Public JSON fields shared by list and send."""
        return {field.name: getattr(self, field.name) for field in fields(Session)}


@dataclass
class TmuxSession(Session):
    active: bool
    delivery_identity: str = ""
