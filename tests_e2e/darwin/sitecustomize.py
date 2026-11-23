"""Test-only: make the CLI under test take its macOS (ps-based) process path.

procps on Linux accepts the same `ps -A -ww -o pid=,ppid=,args=` invocation, so
the macOS parsing code can run against a real process table on a Linux host.
Launches are recorded so the test can prove that the ps path actually ran.
"""
import json
import os
import platform
import sys

if os.environ.pop("AGENT_SESSIONS_E2E_FAKE_DARWIN", None) == "1":
    platform.system = lambda: "Darwin"

_log = os.environ.pop("AGENT_SESSIONS_E2E_EXEC_LOG", None)


def _record(event, args):
    if event == "subprocess.Popen":
        executable, argv, _, _ = args
        record = ["launch", os.fsdecode(executable), argv]
    elif event == "open" and isinstance(args[0], str) and args[0].startswith("/proc/"):
        record = ["proc", args[0]]
    else:
        return
    with open(_log, "a", encoding="utf-8") as handle:
        print(json.dumps(record), file=handle)


if _log:
    sys.addaudithook(_record)
