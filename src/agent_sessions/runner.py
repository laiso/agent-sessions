import json
import subprocess
from collections.abc import Callable

from .model import SourceError

Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]


def run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, timeout=5)


def output(runner: Runner, argv: list[str], empty_errors: tuple[str, ...] = ()) -> str:
    try:
        result = runner(argv)
    except subprocess.TimeoutExpired as exc:
        # TimeoutExpired's string includes argv, which can contain the message
        # and the private tmux delivery identity.
        raise SourceError(f"command timed out after {exc.timeout} seconds") from exc
    except (OSError, UnicodeError) as exc:
        raise SourceError(str(exc)) from exc
    if result.returncode:
        if any(reason in result.stderr for reason in empty_errors):
            return ""
        raise SourceError(json_message(result.stderr) or json_message(result.stdout)
                          or (result.stderr or "").strip() or (result.stdout or "").strip()
                          or f"exit code {result.returncode}")
    return result.stdout


def json_message(text: str | None) -> str | None:
    """error.message from a JSON error like Herdr's, or None."""
    try:
        message = json.loads((text or "").strip())["error"]["message"]
    except (ValueError, KeyError, TypeError):
        return None
    return message if isinstance(message, str) and message else None


def error_message(text: str) -> str:
    """Readable message: a JSON error's message, otherwise the text itself."""
    return json_message(text) or (text or "").strip()
