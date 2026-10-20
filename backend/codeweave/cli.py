"""Command-line interface for the local CodeWeave service and index."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys
import uuid

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
    serve.add_argument('--workspace-state',type=Path)
    serve.add_argument('--allow-workspace-refresh',action='store_true')
    workspace = subparsers.add_parser('workspace',help='Configure and refresh a saved local workspace')
    workspace.add_argument('action',choices=['configure','status','refresh','head','attempt','cancel'])
    workspace.add_argument('request_id',nargs='?')
    workspace.add_argument('--state-dir',type=Path,required=True)
    workspace.add_argument('--project-root',type=Path)
    workspace.add_argument('--compilation-database',type=Path)
    workspace.add_argument('--head-compilation-database',type=Path)
    workspace.add_argument('--analyzer',type=Path)
    workspace.add_argument('--resource-dir',type=Path)
    workspace.add_argument('--dependency-root',type=Path,action='append',default=[])
    workspace.add_argument('--toolchain-root',type=Path,action='append',default=[])
    workspace.add_argument('--generation',type=int)
    workspace.add_argument('--token')
    workspace.add_argument('--background',action='store_true')
    workspace.add_argument('--details',choices=['units'])
    workspace.add_argument('--limit',type=int,default=20)
    workspace.add_argument('--offset',type=int,default=0)
    workspace.add_argument('--field',choices=['message','source_file','command','diagnostics'])
    workspace.add_argument('--field-offset',type=int,default=0)
    opened=subparsers.add_parser('open',help='Initialize/reopen a workspace and serve its actual map')
    opened.add_argument('--state-dir',type=Path,required=True)
    opened.add_argument('--project-root',type=Path)
    opened.add_argument('--compilation-database',type=Path)
    opened.add_argument('--head-compilation-database',type=Path)
    opened.add_argument('--analyzer',type=Path)
    opened.add_argument('--resource-dir',type=Path)
    opened.add_argument('--dependency-root',type=Path,action='append',default=[])
    opened.add_argument('--toolchain-root',type=Path,action='append',default=[])
    opened.add_argument('--frontend',type=Path,required=True)
    opened.add_argument('--port',type=int,default=8000)
    opened.add_argument('--no-serve',action='store_true',help='Initialize/configure once, emit JSON and exit')
    migration=subparsers.add_parser('migrate',help='Copy a schema3 database to a new schema4 path')
    migration.add_argument('--from',dest='source',type=Path,required=True)
    migration.add_argument('--to',dest='destination',type=Path,required=True)
    index_parser = subparsers.add_parser("index", help="Build a persistent SQLite index", exit_on_error=False)
    index_parser.add_argument("--compilation-database", type=Path, required=True, help="Directory containing compile_commands.json")
    index_parser.add_argument("--database", type=Path, required=True, help="SQLite database path")
    index_parser.add_argument("--analyzer", type=Path, required=True, help="Built codeweave-analyzer executable")
    index_parser.add_argument("--project-root", type=Path, required=True)
    index_parser.add_argument("--dependency-root", type=Path, action="append", default=[])
    index_parser.add_argument("--toolchain-root", type=Path, action="append", default=[])
    index_parser.add_argument("--resource-dir", type=Path, help="Trusted Clang resource directory containing include/")
    query_parser = subparsers.add_parser("query", help="Query a persistent index", exit_on_error=False)
    query_parser.add_argument("query_command", choices=["status", "indexes", "search", "show", "callers", "callees", "neighbors", "source",'diff','diff-symbols','diff-calls','diff-files','diff-source'])
    query_parser.add_argument("value", nargs="?")
    query_parser.add_argument("--database", type=Path, required=True)
    query_parser.add_argument("--index-id")
    query_parser.add_argument("--limit", type=int, default=50)
    query_parser.add_argument("--details", choices=["units", "inputs"], help="Paged status details")
    query_parser.add_argument("--offset", type=int, default=0, help="Page row offset")
    query_parser.add_argument("--field", choices=["command", "diagnostics", "source_file", "path", "toolchain", "provenance", "manifest"], help="Read one status field in bounded byte chunks")
    query_parser.add_argument("--field-offset", type=int, default=0, help="UTF-8 byte offset within a status field")
    query_parser.add_argument("--start-line", type=int)
    query_parser.add_argument("--end-line", type=int)
    query_parser.add_argument('--left')
    query_parser.add_argument('--right')
    query_parser.add_argument('--change')
    query_parser.add_argument('--left-path')
    query_parser.add_argument('--right-path')
    query_parser.add_argument('--left-start',type=int,default=1)
    query_parser.add_argument('--right-start',type=int,default=1)
    query_parser.add_argument('--lines',type=int,default=40)
    try:
        args = parser.parse_args()
        if args.command == "version":
            _json({"version": __version__})
        elif args.command == "serve":
            import uvicorn
            from codeweave.app import create_app
            if not 1 <= args.port <= 65535:
                raise ValueError("port must be 1..65535")
            uvicorn.run(create_app(args.database, args.frontend,args.workspace_state,args.allow_workspace_refresh), host="127.0.0.1", port=args.port, reload=False)
        elif args.command in {'workspace','open'}:
            from codeweave.workspace import Workspace,configure
            state=args.state_dir
            needs_config=args.command=='workspace' and args.action=='configure' or not (state/'workspace.json').exists()
            if needs_config or args.command=='open' and args.project_root:
                if not all((args.project_root,args.compilation_database,args.analyzer,args.resource_dir)):
                    raise IndexError('missing_argument','first configuration needs project-root, compilation-database, analyzer and resource-dir')
                configure(state,args.project_root,args.compilation_database,args.analyzer,args.resource_dir,args.dependency_root,args.toolchain_root,args.head_compilation_database)
            ws=Workspace(state)
            if args.command=='open':
                ws.recover()
                initial=ws.status()
                if initial['latest_successful_index'] is None:
                    request=ws.enqueue(initial['generation'],str(uuid.uuid4()))
                    initial_attempt=ws.run(request['request_id'])
                    if args.no_serve and initial_attempt['state']!='published':
                        raise IndexError(initial_attempt['error_code'] or 'analysis_failed',f"request {request['request_id']}: {initial_attempt['message']}")
                if args.no_serve:
                    _json(ws.status())
                else:
                    if not 1<=args.port<=65535:raise IndexError('invalid_arguments','port must be 1..65535')
                    import uvicorn
                    from codeweave.app import create_app
                    uvicorn.run(create_app(None,args.frontend,state,True),host='127.0.0.1',port=args.port,reload=False)
            elif args.action in {'configure','status'}:
                _json(ws.status())
            elif args.action in {'refresh','head'}:
                generation=ws.status()['generation'] if args.generation is None else args.generation
                request=ws.enqueue(generation,args.token or str(uuid.uuid4()),'head' if args.action=='head' else 'workspace')
                if args.background:
                    if request.pop('created',False):ws.spawn(request['request_id'])
                    _json(request)
                else:
                    result=ws.run(request['request_id']) if request.pop('created',False) else request
                    if result['state'] in {'failed','cancelled','interrupted'}:
                        raise IndexError(result['error_code'] or 'analysis_failed',f"request {result['request_id']}: {result['message']}")
                    _json(result)
            elif args.action in {'attempt','cancel'}:
                if not args.request_id:raise IndexError('missing_argument','request ID is required')
                _json(ws.cancel(args.request_id) if args.action=='cancel' else ws.attempt(args.request_id,args.limit,args.offset,args.details=='units',args.field,args.field_offset))
        elif args.command=='migrate':
            from codeweave.schema import migrate
            _json(migrate(args.source,args.destination))
        elif args.command == "index":
            _json(build_index(args.database, args.compilation_database, args.analyzer, args.project_root, args.dependency_root, args.toolchain_root, args.resource_dir))
        elif args.command == "query":
            if args.query_command.startswith('diff'):
                if not args.left or not args.right:raise IndexError('missing_argument','comparison requires --left and --right index IDs')
                from codeweave.diff import compare,source_diff
                if args.query_command=='diff-source':
                    _json(source_diff(args.database,args.left,args.right,args.left_path,args.right_path,args.left_start,args.right_start,args.lines))
                else:
                    kind={'diff':'summary','diff-symbols':'symbols','diff-calls':'calls','diff-files':'files'}[args.query_command]
                    _json(compare(args.database,args.left,args.right,kind,args.limit,args.offset,args.change))
            else:
                _json(query(args.database, args.query_command, args.value, args.index_id, args.limit, args.start_line, args.end_line, args.details, args.offset, args.field, args.field_offset))
    except IndexError as error:
        _fail(error.code, str(error))
    except (OSError, ValueError, sqlite3.Error, argparse.ArgumentError) as error:
        _fail("invalid_arguments", str(error))
