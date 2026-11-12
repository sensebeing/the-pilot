"""Invoke the installed standalone runtime using the selected configuration root."""
import os
from pathlib import Path
import subprocess
import sys

from .defaults import ENGINE_NAME, PILOT_SUBDIR


def main(argv: list[str]) -> int:
    root_value = (
        os.environ.get("THE_PILOT_GEMINI_DIR", "").strip()
        or os.environ.get("ORCA_AUTOPILOT_GEMINI_DIR", "").strip()
    )
    root = Path(root_value or str(Path.home() / ".gemini")).expanduser()
    engine = root / PILOT_SUBDIR / ENGINE_NAME
    if not engine.is_file():
        print(f"Pilot is not installed at {root}; run pilot apply first.", file=sys.stderr)
        return 2
    return subprocess.run([sys.executable, str(engine), *argv]).returncode
