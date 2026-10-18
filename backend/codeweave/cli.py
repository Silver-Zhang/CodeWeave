"""Command-line interface for the local CodeWeave service and index."""
import argparse
import json
from pathlib import Path

from codeweave import __version__
from codeweave.index import IndexError, build_index, query


def _json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


def main() -> None:
    parser = argparse.ArgumentParser(prog="codeweave", description="Local CodeWeave code map")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("version", help="Show package version")
    subparsers.add_parser("serve", help="Start local API at 127.0.0.1:8000")
    index_parser = subparsers.add_parser("index", help="Build a persistent SQLite index")
    index_parser.add_argument("--compilation-database", type=Path, required=True, help="Directory containing compile_commands.json")
    index_parser.add_argument("--database", type=Path, required=True, help="SQLite database path")
    index_parser.add_argument("--analyzer", type=Path, required=True, help="Built codeweave-analyzer executable")
    query_parser = subparsers.add_parser("query", help="Query a persistent index")
    query_parser.add_argument("query_command", choices=["status", "search", "show", "callers", "callees", "source"])
    query_parser.add_argument("value", nargs="?")
    query_parser.add_argument("--database", type=Path, required=True)
    query_parser.add_argument("--index-id")
    query_parser.add_argument("--limit", type=int, default=50)
    query_parser.add_argument("--start-line", type=int)
    query_parser.add_argument("--end-line", type=int)
    args = parser.parse_args()
    try:
        if args.command == "version":
            print(__version__)
        elif args.command == "serve":
            import uvicorn
            uvicorn.run("codeweave.app:app", host="127.0.0.1", port=8000, reload=False)
        elif args.command == "index":
            _json(build_index(args.database, args.compilation_database, args.analyzer))
        elif args.command == "query":
            _json(query(args.database, args.query_command, args.value, args.index_id, args.limit, args.start_line, args.end_line))
    except (IndexError, OSError, ValueError) as error:
        parser.exit(2, f"codeweave: {error}\n")
