"""Invoke the installed standalone runtime using the selected configuration root."""
import os
from pathlib import Path
import subprocess
import sys


def main(argv: list[str]) -> int:
    root = Path(os.environ.get("ORCA_AUTOPILOT_GEMINI_DIR", str(Path.home() / ".gemini"))).expanduser()
    engine = root / "config/autopilot/autopilot.py"
    if not engine.is_file():
        print(f"Pilot is not installed at {root}; run pilot apply first.", file=sys.stderr)
        return 2
    return subprocess.run([sys.executable, str(engine), *argv]).returncode
