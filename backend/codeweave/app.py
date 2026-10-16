"""Local API scaffold. No source analysis or graph query is implemented yet."""
from fastapi import FastAPI
from codeweave import __version__

app = FastAPI(title="CodeWeave", version=__version__)

@app.get("/api/health")
def health() -> dict[str, object]:
    """Report only capabilities actually implemented in this bootstrap."""
    return {
        "status": "ok", "package_version": __version__, "stage": "bootstrap",
        "capabilities": {
            "clang_indexing": False,
            "structural_queries": False,
            "interactive_graph": False,
            "workspace_updates": False,
            "git_diff": False,
            "runtime_trace": False,
        },
    }
