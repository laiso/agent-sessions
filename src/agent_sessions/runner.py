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
    except (OSError, UnicodeError, subprocess.TimeoutExpired) as exc:
        raise SourceError(str(exc)) from exc
    if result.returncode:
        if any(reason in result.stderr for reason in empty_errors):
            return ""
        raise SourceError(error_message(result.stderr) or error_message(result.stdout)
                          or f"exit code {result.returncode}")
    return result.stdout


def error_message(text: str) -> str:
    """Return a readable message; JSON errors like Herdr's use their error.message."""
    text = (text or "").strip()
    try:
        message = json.loads(text)["error"]["message"]
        if isinstance(message, str) and message:
            return message
    except (ValueError, KeyError, TypeError):
        pass
    return text
