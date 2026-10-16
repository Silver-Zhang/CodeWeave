"""Create a temporary two-commit C++ fixture repository without touching RMC."""
import argparse
from pathlib import Path
import shutil
import subprocess
import tempfile

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "cpp_versions"

def run(*args: str, cwd: Path) -> str:
    return subprocess.check_output(args, cwd=cwd, text=True).strip()

def create(destination: Path) -> tuple[str, str]:
    if destination.exists():
        raise FileExistsError(f"Refusing to overwrite {destination}")
    destination.mkdir(parents=True)
    run("git", "init", "-b", "main", cwd=destination)
    run("git", "config", "user.name", "CodeWeave Fixture", cwd=destination)
    run("git", "config", "user.email", "codeweave-fixture@example.invalid", cwd=destination)
    commits = []
    for version in ("v1", "v2"):
        for name in ("CMakeLists.txt", "main.cpp"):
            shutil.copyfile(FIXTURES / version / name, destination / name)
        run("git", "add", "CMakeLists.txt", "main.cpp", cwd=destination)
        run("git", "commit", "-m", f"fixture {version}", cwd=destination)
        commits.append(run("git", "rev-parse", "HEAD", cwd=destination))
    return commits[0], commits[1]

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--destination", type=Path, help="New folder, default temp")
    args = parser.parse_args()
    path = args.destination or Path(tempfile.mkdtemp(prefix="codeweave-fixture-root-")) / "fixture"
    first, second = create(path)
    print(f"repo={path.resolve()}")
    print(f"base={first}")
    print(f"target={second}")

if __name__ == "__main__":
    main()
