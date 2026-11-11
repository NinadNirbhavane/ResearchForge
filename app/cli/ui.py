"""`researchforge ui` — launch the local browser workspace (UI-01)."""
from pathlib import Path

from app.ui import server as ui_server


def cmd_ui(args, root=None) -> int:
    root = root or Path(__file__).resolve().parents[2]
    try:
        ui_server.serve(root, port=args.port,
                        open_browser=not args.no_open,
                        app_window=args.app_window)
    except OSError as exc:
        print(f"[ERROR] cannot start the local server: {exc}")
        print("Hint: choose another port with --port <number>.")
        return 1
    return 0


def setup(subparsers):
    parser = subparsers.add_parser(
        "ui", help="launch the local browser workspace (127.0.0.1 only)",
        epilog=("The server binds to 127.0.0.1 and prints its URL. "
                "Stop it with Ctrl+C. Nothing is approved automatically."))
    parser.add_argument("--port", type=int, default=ui_server.DEFAULT_PORT,
                        help=f"local port (default: {ui_server.DEFAULT_PORT})")
    parser.add_argument("--no-open", action="store_true",
                        help="do not open the browser automatically")
    parser.add_argument("--app-window", action="store_true",
                        help="open an application-style Edge window when "
                             "Edge is installed")
    parser.set_defaults(handler=cmd_ui)
