"""Command dispatcher: ``uv run python -m scripts.analysis.video <command> [options]``."""

from __future__ import annotations

import importlib
import sys

COMMANDS = {
    "fetch": "sources",
    "horns": "horns",
    "instants": "instants",
    "frames": "frames",
    "observe": "observe",
    "facts": "facts",
    "reel": "reel",
    "report": "report",
}


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help") or args[0] not in COMMANDS:
        print("usage: python -m scripts.analysis.video <command> [options]\n")
        print("commands (in pipeline order):")
        for name, mod in COMMANDS.items():
            doc = importlib.import_module(f"scripts.analysis.video.{mod}").__doc__ or ""
            print(f"  {name:10} {doc.strip().splitlines()[0]}")
        return 0 if args and args[0] in ("-h", "--help") else 2
    module = importlib.import_module(f"scripts.analysis.video.{COMMANDS[args[0]]}")
    return int(module.main(args[1:]))


if __name__ == "__main__":
    raise SystemExit(main())
