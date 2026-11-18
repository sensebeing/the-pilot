"""Unified installer and runtime command entry point."""
import sys
from . import installer, runtime


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    delegated = installer.maybe_delegate_to_checkout(args)
    if delegated is not None:
        return delegated
    if args and args[0] in {"worker", "mode", "hook", "sync", "set", "log"}:
        return runtime.main(args)
    return installer.main(args)


if __name__ == "__main__":
    raise SystemExit(main())
