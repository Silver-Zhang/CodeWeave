"""Command-line interface for the local CodeWeave service and index."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys

from codeweave import __version__
from codeweave.index import IndexError, build_index, query


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        _fail("invalid_arguments", message)


def _json(value: object, stream: object = sys.stdout) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True), file=stream)


def _fail(code: str, message: str, exit_code: int = 2) -> None:
    _json({"error": {"code": code, "message": message}}, sys.stderr)
    raise SystemExit(exit_code)


def main() -> None:
    parser = JsonArgumentParser(prog="codeweave", description="Local CodeWeave code map", exit_on_error=False)
    subparsers = parser.add_subparsers(dest="command", required=True, parser_class=JsonArgumentParser)
    subparsers.add_parser("version", help="Show package version")
    serve = subparsers.add_parser("serve", help="Start the read-only local API")
    serve.add_argument("--database", type=Path, required=True)
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--frontend", type=Path, help="Trusted Vite dist directory to serve at /")
    index_parser = subparsers.add_parser("index", help="Build a persistent SQLite index", exit_on_error=False)
    index_parser.add_argument("--compilation-database", type=Path, required=True, help="Directory containing compile_commands.json")
    index_parser.add_argument("--database", type=Path, required=True, help="SQLite database path")
    index_parser.add_argument("--analyzer", type=Path, required=True, help="Built codeweave-analyzer executable")
    index_parser.add_argument("--project-root", type=Path, required=True)
    index_parser.add_argument("--dependency-root", type=Path, action="append", default=[])
    index_parser.add_argument("--toolchain-root", type=Path, action="append", default=[])
    index_parser.add_argument("--resource-dir", type=Path, help="Trusted Clang resource directory containing include/")
    query_parser = subparsers.add_parser("query", help="Query a persistent index", exit_on_error=False)
    query_parser.add_argument("query_command", choices=["status", "indexes", "search", "show", "callers", "callees", "neighbors", "source"])
    query_parser.add_argument("value", nargs="?")
    query_parser.add_argument("--database", type=Path, required=True)
    query_parser.add_argument("--index-id")
    query_parser.add_argument("--limit", type=int, default=50)
    query_parser.add_argument("--details", choices=["units", "inputs"], help="Paged status details")
    query_parser.add_argument("--offset", type=int, default=0, help="Page row offset")
    query_parser.add_argument("--field", choices=["command", "diagnostics", "source_file", "path", "toolchain"], help="Read one status field in bounded byte chunks")
    query_parser.add_argument("--field-offset", type=int, default=0, help="UTF-8 byte offset within a status field")
    query_parser.add_argument("--start-line", type=int)
    query_parser.add_argument("--end-line", type=int)
    try:
        args = parser.parse_args()
        if args.command == "version":
            _json({"version": __version__})
        elif args.command == "serve":
            import uvicorn
            from codeweave.app import create_app
            if not 1 <= args.port <= 65535:
                raise ValueError("port must be 1..65535")
            uvicorn.run(create_app(args.database, args.frontend), host="127.0.0.1", port=args.port, reload=False)
        elif args.command == "index":
            _json(build_index(args.database, args.compilation_database, args.analyzer, args.project_root, args.dependency_root, args.toolchain_root, args.resource_dir))
        elif args.command == "query":
            _json(query(args.database, args.query_command, args.value, args.index_id, args.limit, args.start_line, args.end_line, args.details, args.offset, args.field, args.field_offset))
    except IndexError as error:
        _fail(error.code, str(error))
    except (OSError, ValueError, sqlite3.Error, argparse.ArgumentError) as error:
        _fail("invalid_arguments", str(error))
