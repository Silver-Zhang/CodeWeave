"""The baseline CLI intentionally implements only local server startup/version."""
import argparse
from codeweave import __version__

def main() -> None:
    parser = argparse.ArgumentParser(prog="codeweave", description="CodeWeave")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("version", help="Show package version")
    subparsers.add_parser("serve", help="Start local API at 127.0.0.1:8000")
    args = parser.parse_args()
    if args.command == "version":
        print(__version__)
    elif args.command == "serve":
        import uvicorn
        uvicorn.run("codeweave.app:app", host="127.0.0.1", port=8000, reload=False)
