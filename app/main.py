"""ResearchForge CLI entry point (RF-007).

Framework only: argument parsing, version, help, error handling. The
workflow commands (status, sources, literature, research, draft, review,
diagram, export, usage) are registered in app.cli.COMMANDS by their own
later tasks; nothing is faked until then.

Registration convention (for future tasks): a command module exposes
``setup(subparsers)`` which adds its subparser via ``add_parser`` and
attaches its handler with ``parser.set_defaults(handler=func)``.
"""
import argparse
import sys

from app import __version__
from app.cli import COMMANDS


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="researchforge",
        description="ResearchForge - local-first research management and "
        "multi-model orchestration for the smart-glasses walkability study.",
        epilog="Run 'python -m app.main --help' to see available commands.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version="%(prog)s " + __version__,
        help="show the ResearchForge version and exit",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")
    for name, setup in sorted(COMMANDS.items()):
        setup(subparsers)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    handler = getattr(args, "handler", None)
    if handler is None:
        # A registered command without a handler would be a fake success
        # path; we fail loudly instead.
        parser.error(f"command not implemented: {args.command}")
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
