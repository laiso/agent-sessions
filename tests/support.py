import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def fixture(name):
    return json.loads((ROOT / "tests" / "fixtures" / (name + ".json")).read_text())


def response(stdout="", stderr="", code=0):
    return subprocess.CompletedProcess([], code, stdout, stderr)
