"""Check public bootstrap hygiene; this is not a complete secret scanner."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_ROOTS = {"CodeWeave-Workflow", "workflow", "tasks", "records", "inputs",
                   "artifacts", "third_party_private"}

def main() -> int:
    missing = [p for p in ("LICENSE", "README.md", "AGENTS.md", "backend/pyproject.toml")
               if not (ROOT / p).is_file()]
    forbidden = [p for p in FORBIDDEN_ROOTS if (ROOT / p).exists()]
    nested = [str(p.relative_to(ROOT)) for p in ROOT.rglob(".git") if p != ROOT / ".git"]
    errors = [*(f"Missing: {p}" for p in missing),
              *(f"Development/private directory in source: {p}" for p in forbidden),
              *(f"Nested repository: {p}" for p in nested)]
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print("Bootstrap repository boundary checks passed (not a full secret scan).")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
