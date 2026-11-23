"""Record subprocess launches from the CLI under test, without replacing them."""
import json
import os
import sys

log_path = os.environ.pop("AGENT_SESSIONS_E2E_EXEC_LOG", None)


def record_launch(event, args):
    if event == "subprocess.Popen":
        executable, argv, _, _ = args
        with open(log_path, "a", encoding="utf-8") as log:
            print(json.dumps([os.fsdecode(executable), argv]), file=log)


if log_path:
    sys.addaudithook(record_launch)
