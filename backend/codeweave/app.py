"""Local read-only HTTP adapter over the CLI's versioned query kernel."""
from pathlib import Path
import json
import secrets
import sqlite3

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from codeweave import __version__
from codeweave.index import IndexError, query


def create_app(database: Path | None = None, frontend: Path | None = None,
               workspace_state: Path | None = None, allow_workspace_refresh: bool = False) -> FastAPI:
    app = FastAPI(title="CodeWeave", version=__version__, docs_url=None, redoc_url=None)
    # Paths are trusted startup configuration, never selected by HTTP clients.
    from codeweave.workspace import Workspace
    workspace = Workspace(workspace_state) if workspace_state else None
    if allow_workspace_refresh and workspace is None:
        raise IndexError('invalid_arguments', 'workspace refresh requires a configured local workspace')
    if workspace and database is not None and database.resolve() != workspace.database.resolve():
        raise IndexError('configuration_conflict', 'workspace and database paths do not match')
    configured = workspace.database if workspace else database.absolute() if database is not None else None
    csrf_token = secrets.token_urlsafe(32) if allow_workspace_refresh else None
    if workspace and allow_workspace_refresh:
        workspace.recover()
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]"])

    def failure(code: str, message: str, status: int) -> JSONResponse:
        return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)

    @app.middleware("http")
    async def local_boundary(request: Request, call_next):
        origin = request.headers.get("origin")
        if origin and origin != f"{request.url.scheme}://{request.headers.get('host', '')}":
            return failure("origin_forbidden", "cross-origin requests are not enabled; use the same-origin UI", 403)
        response = await call_next(request)
        if response.status_code == 400 and response.headers.get("content-type", "").startswith("text/plain"):
            return failure("host_forbidden", "use the local service hostname", 400)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers['X-Frame-Options']='DENY'
        response.headers['Content-Security-Policy']="frame-ancestors 'none'; object-src 'none'; base-uri 'self'"
        return response

    @app.exception_handler(IndexError)
    async def index_failure(request, error):
        status = 404 if error.code in {"database_not_found", "index_not_found", "symbol_not_found", "source_not_registered", "status_item_not_found"} else 400
        if error.code in {"database_error", "schema_incompatible", "index_unconfigured", 'source_integrity_error'}:
            status = 503
        if error.code == 'source_not_utf8':
            status = 422
        if error.code in {"query_limit", "source_limit", "status_limit"}:
            status = 413
        if error.code in {'refresh_busy','workspace_generation_conflict','idempotency_conflict','configuration_conflict','attempt_finished'}:
            status = 409
        if error.code in {'refresh_forbidden','token_forbidden'}:
            status = 403
        if error.code == 'attempt_not_found':
            status = 404
        return failure(error.code, str(error), status)

    @app.exception_handler(RequestValidationError)
    async def validation_failure(request, error):
        return failure("invalid_arguments", "invalid or missing query parameters", 422)

    @app.exception_handler(HTTPException)
    async def http_failure(request, error):
        return failure("http_error", "route or method is not available", error.status_code)

    @app.exception_handler(Exception)
    async def unexpected_failure(request, error):
        return failure("internal_error", "request failed", 500)

    def run(command, value=None, **kwargs):
        if configured is None:
            raise IndexError("index_unconfigured", "start with codeweave serve --database <index>")
        try:
            return query(configured, command, value, **kwargs)
        except (sqlite3.Error, OSError, ValueError, TypeError, KeyError) as error:
            raise IndexError("database_error", "index query failed; verify database integrity") from error

    @app.get("/api/health")
    def health():
        return {"status": "ok", "package_version": __version__, "stage": "m2a" if workspace else "m1",
                "capabilities": {"structural_queries": True, "interactive_graph": True,
                                 "workspace_updates": bool(workspace and allow_workspace_refresh), "git_diff": bool(workspace), "runtime_trace": False},
                "database_configured": configured is not None}

    @app.get("/api/status")
    def status(index_id: str | None = Query(None, max_length=128), limit: int = 50, offset: int = 0,
               details: str | None = None, field: str | None = None, field_offset: int = 0):
        return run("status", index_id=index_id, limit=limit, offset=offset, details=details, field=field, field_offset=field_offset)

    @app.get("/api/indexes")
    def indexes(limit: int = 20, offset: int = 0):
        return run("indexes", limit=limit, offset=offset)

    @app.get("/api/search")
    def search(index_id: str = Query(..., min_length=1, max_length=128), q: str = Query("", max_length=1024), limit: int = 20, offset: int = 0):
        return run("search", q, index_id=index_id, limit=limit, offset=offset)

    @app.get("/api/symbol")
    def symbol(index_id: str = Query(..., min_length=1, max_length=128), usr: str = Query(..., min_length=1, max_length=16384)):
        result = run("show", usr, index_id=index_id)
        if not result["items"]:
            raise IndexError("symbol_not_found", "symbol is not present in this index")
        return result

    @app.get("/api/calls")
    def calls(index_id: str = Query(..., min_length=1, max_length=128), usr: str = Query(..., min_length=1, max_length=16384),
              direction: str = Query(..., pattern="^(callers|callees)$"), limit: int = 20, offset: int = 0):
        symbol(index_id, usr)
        return run(direction, usr, index_id=index_id, limit=limit, offset=offset)

    @app.get("/api/neighbors")
    def neighbors(index_id: str = Query(..., min_length=1, max_length=128), usr: str = Query(..., min_length=1, max_length=16384), limit: int = 12):
        return run("neighbors", usr, index_id=index_id, limit=limit)

    @app.get("/api/source")
    def source(index_id: str = Query(..., min_length=1, max_length=128), path: str = Query(..., min_length=1, max_length=16384),
               start_line: int = Query(..., ge=1), end_line: int = Query(..., ge=1), clamp_end: bool = False):
        return run("source", path, index_id=index_id, start_line=start_line, end_line=end_line, clamp_end=clamp_end)

    @app.get('/api/session')
    def session():
        return {'workspace_refresh_enabled': bool(workspace and allow_workspace_refresh), 'csrf_token': csrf_token}

    @app.get('/api/workspace')
    def workspace_status():
        return {**workspace.status(), 'refresh_enabled': allow_workspace_refresh} if workspace else {'configured': False, 'refresh_enabled': False}

    def require_workspace():
        if workspace is None:
            raise IndexError('index_unconfigured','start with a trusted workspace configuration')
        return workspace

    async def mutation(request: Request) -> dict:
        if not workspace or not allow_workspace_refresh:
            raise IndexError('refresh_forbidden','workspace writes are disabled by trusted startup configuration')
        token = request.headers.get('x-codeweave-token','')
        if not csrf_token or not secrets.compare_digest(token,csrf_token):
            raise IndexError('token_forbidden','a same-origin session token is required')
        if request.headers.get('content-type','').split(';',1)[0].strip().lower() != 'application/json':
            raise IndexError('invalid_arguments','workspace mutations require application/json')
        data=bytearray()
        async for chunk in request.stream():
            if len(data)+len(chunk)>4096:
                raise IndexError('query_limit','workspace request exceeds 4096 bytes')
            data.extend(chunk)
        try:
            value=json.loads(data)
        except ValueError as error:
            raise IndexError('invalid_arguments','request body must be JSON') from error
        if not isinstance(value,dict):
            raise IndexError('invalid_arguments','request body must be an object')
        return value

    @app.post('/api/workspace/refresh',status_code=202)
    async def refresh(request: Request):
        body=await mutation(request)
        if set(body)-{'generation','idempotency_token','source_kind'} or type(body.get('generation')) is not int or not isinstance(body.get('idempotency_token'),str):
            raise IndexError('invalid_arguments','expected generation and idempotency_token; paths/commands cannot be supplied')
        ws=require_workspace()
        result=ws.enqueue(body['generation'],body['idempotency_token'],body.get('source_kind','workspace'))
        if result.pop('created',False):
            ws.spawn(result['request_id'])
        return result

    @app.get('/api/workspace/attempts/{request_id}')
    def attempt(request_id: str,limit: int = 20,offset: int = 0,details: str | None = None,
                field: str | None = None,field_offset: int = 0):
        if details not in {None,'units'}:
            raise IndexError('invalid_arguments','attempt details must be units')
        return require_workspace().attempt(request_id,limit,offset,details=='units',field,field_offset)

    @app.post('/api/workspace/attempts/{request_id}/cancel')
    async def cancel(request_id: str,request: Request):
        body=await mutation(request)
        if body:
            raise IndexError('invalid_arguments','cancel takes an empty JSON object')
        return require_workspace().cancel(request_id)

    def comparison(left: str,right: str,kind: str,limit: int,offset: int,change: str | None):
        if configured is None:
            raise IndexError('index_unconfigured','start with a configured database')
        from codeweave.diff import compare
        return compare(configured,left,right,kind,limit,offset,change)

    @app.get('/api/diff')
    def diff_summary(left: str = Query(...,min_length=1,max_length=128),right: str = Query(...,min_length=1,max_length=128)):
        return comparison(left,right,'summary',20,0,None)

    @app.get('/api/diff/{kind}')
    def diff_page(kind: str,left: str = Query(...,min_length=1,max_length=128),right: str = Query(...,min_length=1,max_length=128),
                  limit: int = 20,offset: int = 0,change: str | None = Query(None,max_length=128)):
        return comparison(left,right,kind,limit,offset,change)

    @app.get('/api/source-diff')
    def diff_source(left: str = Query(...,min_length=1,max_length=128),right: str = Query(...,min_length=1,max_length=128),
                    left_path: str | None = Query(None,max_length=16384),right_path: str | None = Query(None,max_length=16384),
                    left_start: int = 1,right_start: int = 1,lines: int = 40):
        if configured is None:
            raise IndexError('index_unconfigured','start with a configured database')
        from codeweave.diff import source_diff
        return source_diff(configured,left,right,left_path,right_path,left_start,right_start,lines)

    if frontend is not None:
        if not (frontend / "index.html").is_file():
            raise IndexError("invalid_path", "frontend must be a built Vite dist directory")
        app.mount("/", StaticFiles(directory=frontend, html=True), name="frontend")
    return app


# Import remains harmless; only the public serve command supplies trusted paths.
app = create_app()
