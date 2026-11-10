"""Unified installer and runtime command entry point."""
import sys
from . import installer, runtime


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in {"worker", "mode", "hook", "sync", "set", "log"}:
        return runtime.main(args)
    return installer.main(args)
