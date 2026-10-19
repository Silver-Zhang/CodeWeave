"""Local read-only HTTP adapter over the CLI's versioned query kernel."""
from pathlib import Path
import sqlite3

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from codeweave import __version__
from codeweave.index import IndexError, query


def create_app(database: Path | None = None, frontend: Path | None = None) -> FastAPI:
    app = FastAPI(title="CodeWeave", version=__version__, docs_url=None, redoc_url=None)
    # Paths are trusted startup configuration, never selected by HTTP clients.
    configured = database.absolute() if database is not None else None
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
        return response

    @app.exception_handler(IndexError)
    async def index_failure(request, error):
        status = 404 if error.code in {"database_not_found", "index_not_found", "symbol_not_found", "source_not_registered", "status_item_not_found"} else 400
        if error.code in {"database_error", "schema_incompatible", "index_unconfigured"}:
            status = 503
        if error.code in {"query_limit", "source_limit", "status_limit"}:
            status = 413
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
        return {"status": "ok", "package_version": __version__, "stage": "m1",
                "capabilities": {"structural_queries": True, "interactive_graph": True,
                                 "workspace_updates": False, "git_diff": False, "runtime_trace": False},
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

    if frontend is not None:
        if not (frontend / "index.html").is_file():
            raise IndexError("invalid_path", "frontend must be a built Vite dist directory")
        app.mount("/", StaticFiles(directory=frontend, html=True), name="frontend")
    return app


# Import remains harmless; only the public serve command supplies trusted paths.
app = create_app()
