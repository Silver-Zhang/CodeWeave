"""A: synthetic contracts; B: real AST; C: fresh-process public sandbox CLI.

All inputs and sentinels are owned temporary files. No test substitutes facts or
mocks Bubblewrap in B/C. Synchronization wrappers only pause real analyzer I/O.
"""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

import pytest

from codeweave.index import IndexError, SCHEMA, _collect, query

ROOT = Path(__file__).resolve().parents[2]
ANALYZER = Path(os.environ.get("CODEWEAVE_ANALYZER", ROOT / ".build/analyzer/codeweave-analyzer"))


LEGACY_SCHEMA = '\n    PRAGMA foreign_keys = ON;\n    CREATE TABLE IF NOT EXISTS indexes (\n      id TEXT PRIMARY KEY, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,\n      compilation_database TEXT NOT NULL, compilation_sha256 TEXT NOT NULL, coverage TEXT NOT NULL, diagnostic TEXT\n    );\n    CREATE TABLE IF NOT EXISTS files (\n      index_id TEXT NOT NULL REFERENCES indexes(id), path TEXT NOT NULL, content TEXT NOT NULL,\n      sha256 TEXT NOT NULL, PRIMARY KEY (index_id, path)\n    );\n    CREATE TABLE IF NOT EXISTS symbols (\n      index_id TEXT NOT NULL REFERENCES indexes(id), usr TEXT NOT NULL, name TEXT NOT NULL,\n      kind TEXT NOT NULL, declaration_file TEXT NOT NULL, declaration_line INTEGER NOT NULL,\n      declaration_column INTEGER NOT NULL, definition_file TEXT, definition_line INTEGER,\n      definition_column INTEGER, PRIMARY KEY (index_id, usr)\n    );\n    CREATE TABLE IF NOT EXISTS calls (\n      id INTEGER PRIMARY KEY, index_id TEXT NOT NULL REFERENCES indexes(id), caller_usr TEXT,\n      target_usr TEXT, dispatch TEXT NOT NULL, file TEXT NOT NULL, line INTEGER NOT NULL,\n      column_number INTEGER NOT NULL\n    );\n    '


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    return root


def write(root, name, content):
    p = root / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return p


def commands(root, files, extras=()):
    rows = [{"directory": str(root), "file": f, "arguments": ["c++", "-std=c++17", "-Iinc", *extras, "-c", f]} for f in files]
    write(root, "compile_commands.json", json.dumps(rows))
    return rows


def cli(*args, code=0, env=None):
    argv = [sys.executable, "-m", "codeweave", *map(str, args)]
    if args[0] == "index" and os.environ.get("CODEWEAVE_RESOURCE_DIR"):
        argv += ["--resource-dir", os.environ["CODEWEAVE_RESOURCE_DIR"]]
    p = subprocess.run(argv, capture_output=True, text=True, env={**os.environ, "PYTHONPATH": str(ROOT / "backend"), **(env or {})}, timeout=45)
    if os.environ.get("CODEWEAVE_TEST_LOG"):
        with open(os.environ["CODEWEAVE_TEST_LOG"], "a") as log:
            log.write(json.dumps({"argv": argv, "exit": p.returncode, "stdout": p.stdout, "stderr": p.stderr}) + "\n")
    assert p.returncode == code, p.stderr
    assert not (p.stdout if code else p.stderr), (p.stdout, p.stderr)
    return json.loads(p.stderr if code else p.stdout)


def index(root, db, **kwargs):
    return cli("index", "--project-root", root, "--compilation-database", root, "--database", db, "--analyzer", ANALYZER, **kwargs)


def q(db, command, *args, **kwargs):
    return cli("query", command, *args, "--database", db, **kwargs)


@pytest.mark.parametrize("kind", ["legacy", "empty", "corrupt", "version", "forged", "missing"])
def test_A_readonly_schema(tmp_path, kind):
    db = tmp_path / "index.db"
    if kind in {"legacy", "version", "forged"}:
        with sqlite3.connect(db) as c:
            if kind == "legacy":
                c.executescript(LEGACY_SCHEMA)
                c.execute("INSERT INTO indexes(id,compilation_database,compilation_sha256,coverage) VALUES ('keep','own-ccdb','abc','complete')")
                c.execute("INSERT INTO files VALUES ('keep','own.cpp','int a();','abc')")
            else:
                c.executescript("CREATE TABLE indexes(id TEXT); INSERT INTO indexes VALUES('keep'); CREATE TABLE files(path TEXT); CREATE TABLE symbols(usr TEXT); CREATE TABLE calls(id INTEGER);")
            c.execute("PRAGMA user_version=" + str({"legacy": 0, "version": 99, "forged": 3}[kind]))
    elif kind != "missing":
        db.write_bytes(b"bad sqlite" if kind == "corrupt" else b"")
    before = db.read_bytes() if db.exists() else None
    error = q(db, "status", code=2)
    assert error["error"]["code"] in {"schema_incompatible", "database_error", "database_not_found"}
    assert (db.read_bytes() if db.exists() else None) == before


def test_A_buffer_conflict_and_protocol(project):
    source = str(project / "a.cpp")
    data = b"int a();\n"
    file = {"schema": 2, "type": "file", "tu": source, "path": source, "sha256": hashlib.sha256(data).hexdigest(), "content_hex": data.hex()}
    wrong = {**file, "content_hex": b"int b();\n".hex(), "sha256": hashlib.sha256(b"int b();\n").hexdigest()}
    with pytest.raises(IndexError, match="different versions"):
        _collect([file, wrong], [{"file": source}], project)
    with pytest.raises(IndexError, match="unsupported"):
        _collect([{**file, "schema": 1}], [{"file": source}], project)


def test_A_error_diagnostics_rejected(project):
    source = str(project / "a.cpp")
    with pytest.raises(IndexError, match="diagnostics"):
        _collect([{"schema": 2, "type": "unit", "tu": source, "status": "complete", "diagnostics": [{"level": "error", "message": "synthetic driver error"}]}], [{"file": source}], project)


def semantic_fixture(root):
    write(root, "inc/api.hpp", '''#pragma once
int leaf(int); double leaf(double);
static int hidden(int x) { return leaf(x); }
namespace { int anon(int x) { return leaf(x); } }
inline int shared(int x) { return leaf(x); }
int later(int);
struct Base { Base(); virtual ~Base(); virtual int run(int); int normal(int); static int stat(int); int inside(int x) { return leaf(x); } };
template<class T> T simple(T x) { return leaf(x); }
''')
    write(root, "left/unit.cpp", '''#include "api.hpp"
static int local(int x) { return leaf(x); }
int leaf(int x) { return x; } double leaf(double x) { return x; }
Base::Base() { leaf(1); } Base::~Base() { leaf(2); }
int Base::run(int x) { return leaf(x); }
int Base::normal(int x) { return leaf(x); }
int Base::stat(int x) { return leaf(x); }
int left(int x) { return local(x)+hidden(x)+anon(x)+shared(x)+later(x)+leaf(x)+leaf(x+1); }
int uneval() { using X=decltype(leaf(1)); return sizeof(leaf(2))+noexcept(leaf(3))+X(0); }
''')
    write(root, "right/unit.cpp", '''#include "api.hpp"
#define HIT() leaf(1)
#define TWO(x) (leaf(x)+leaf(x))
static int local(int x) { return leaf(x); }
int later(int x) { return leaf(x); }
int right(int x) { return local(x)+hidden(x)+anon(x)+shared(x)+leaf(1.5); }
int macros() { return HIT()+HIT()+TWO(1)+TWO(2); }
int dispatch(Base& b) { auto p=static_cast<int(*)(int)>(&leaf); return b.run(1)+b.Base::run(2)+p(3); }
int lambdas(int x) { auto a=[x] { auto b=[x] { return leaf(x); }; return b(); }; return a(); }
int templ(int x) { return simple(x); }
''')
    commands(root, ["left/unit.cpp", "right/unit.cpp"])


def test_B_C_semantics_and_restart(project, tmp_path):
    semantic_fixture(project)
    db = tmp_path / "index.db"
    result = index(project, db)
    items = q(db, "search", "", "--limit", 200)["items"]
    def symbols(name): return [s for s in items if s["name"] == name]
    def calls(symbol): return q(db, "callees", symbol["usr"])["items"]
    for name in ["local", "hidden", "(anonymous namespace)::anon"]:
        assert len(symbols(name)) == 2, (name, symbols(name))
        assert len({s["usr"] for s in symbols(name)}) == 2
    assert {Path(s["definition_file"]).parent.name for s in symbols("local")} == {"left", "right"}
    assert len(symbols("shared")) == 1
    assert len(calls(symbols("shared")[0])) == 1
    assert len(calls(symbols("shared")[0])[0]["units"]) == 2
    assert len(symbols("leaf")) == 2
    assert symbols("later")[0]["definition_file"] == str(project / "right/unit.cpp")
    for name in ["Base::Base", "Base::~Base", "Base::run", "Base::normal", "Base::stat", "Base::inside"]:
        assert [c["other_name"] for c in calls(symbols(name)[0])] == ["leaf"], name
    assert calls(symbols("uneval")[0]) == []
    assert [c["dispatch"] for c in calls(symbols("dispatch")[0])] == ["virtual", "direct", "unknown-function-pointer"]
    macro = calls(symbols("macros")[0])
    assert len(macro) == 6
    assert len({c["instance_id"] for c in macro}) == 6
    assert all(c["line"] == 7 and c["anchors"]["expansion_chain"] for c in macro)
    outer = calls(symbols("lambdas")[0])
    assert len(outer) == 1 and "operator()" in outer[0]["other_name"]
    middle = q(db, "callees", outer[0]["target_usr"])["items"]
    assert len(middle) == 1 and "operator()" in middle[0]["other_name"]
    inner = q(db, "callees", middle[0]["target_usr"])["items"]
    assert [c["other_name"] for c in inner] == ["leaf"]
    template = calls(symbols("templ")[0])[0]
    assert q(db, "show", template["target_usr"])["items"][0]["definition_file"] == str(project / "inc/api.hpp")
    assert [c["other_name"] for c in q(db, "callees", template["target_usr"])["items"]] == ["leaf"]
    with sqlite3.connect(db) as c:
        assert c.execute("SELECT count(*) FROM calls").fetchone()[0] == result["calls"]
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []
    original = (project / "left/unit.cpp").read_text()
    write(project, "left/unit.cpp", original + "// edited after indexing\n")
    assert "edited" not in q(db, "source", project / "left/unit.cpp", "--start-line", 1, "--end-line", 9)["content"]
    rows = json.loads((project / "compile_commands.json").read_text())
    write(project, "compile_commands.json", json.dumps(list(reversed(rows)) + rows[:1]))
    newer = index(project, db)
    assert newer["calls"] == result["calls"] and newer["units"] == 2
    assert q(db, "status", "--index-id", result["index_id"])["index_id"] == result["index_id"]
    assert q(db, "status")["index_id"] == newer["index_id"]


@pytest.mark.parametrize("argument", ["-mnotarealclangoption", "--target=not-a-target", "-include missing.hpp", "syntax", "missing", "partial", "warning"])
def test_C_diagnostics_and_failed_publication(project, tmp_path, argument):
    write(project, "a.cpp", "int good() { return 1; }\n")
    commands(project, ["a.cpp"])
    db = tmp_path / "index.db"
    old = index(project, db)
    if argument == "warning":
        write(project, "a.cpp", '#warning tracked_warning\nint good() { return 2; }\n')
    elif argument in {"syntax", "missing"}:
        write(project, "a.cpp", "int broken( {" if argument == "syntax" else '#include "absent.hpp"\n')
    elif argument == "partial":
        write(project, "b.cpp", "int broken( {")
        commands(project, ["a.cpp", "b.cpp"])
    else:
        commands(project, ["a.cpp"], argument.split())
    if argument == "warning":
        index(project, db)
        status = q(db, "status", "--details", "units")
        assert any(d["level"] == "warning" and "tracked_warning" in d["message"] for u in status["items"] for d in u["diagnostics"])
    else:
        assert index(project, db, code=2)["error"]["code"] == "analysis_failed"
        assert q(db, "status")["index_id"] == old["index_id"]
    assert "return 1" in q(db, "source", project / "a.cpp", "--start-line", 1, "--end-line", 1, "--index-id", old["index_id"])["content"]


def test_C_configurations(project, tmp_path):
    write(project, "a.cpp", "int a() { return 1; }\n")
    write(project, "b.cpp", "int b() { return 2; }\n")
    rows = commands(project, ["a.cpp", "b.cpp"])
    rows[1]["arguments"].insert(1, "-DOTHER=1")
    write(project, "compile_commands.json", json.dumps(rows + rows[:1]))
    db = tmp_path / "index.db"
    assert index(project, db)["units"] == 2
    rows.append({**rows[0], "arguments": [*rows[0]["arguments"], "-DALTERNATE=1"]})
    write(project, "compile_commands.json", json.dumps(rows))
    assert index(project, db, code=2)["error"]["code"] == "multiple_configurations"


@pytest.mark.parametrize("option", ["-serialize-diagnostics", "--serialize-diagnostics", "--serialize-diagnostics=OUT", "-Xclang", "@OUT", "-Wp,-MD,OUT", "-MFOUT", "-fplugin=OUT", "-fmodules-cache-path=OUT"])
def test_C_unsafe_arguments(project, tmp_path, option):
    sentinel = write(tmp_path, "sentinel", "KEEP")
    write(project, "a.cpp", "int a() { return 1; }\n")
    extras = [option.replace("OUT", str(sentinel))]
    if option in {"-serialize-diagnostics", "--serialize-diagnostics", "-Xclang"}:
        extras.append(str(sentinel))
    commands(project, ["a.cpp"], extras)
    assert index(project, tmp_path / "index.db", code=2)["error"]["code"] == "unsafe_compilation_argument"
    assert sentinel.read_text() == "KEEP"


def test_C_roots_line_and_dependencies(project, tmp_path):
    private = write(tmp_path, "private.hpp", '#error OWN_PRIVATE_SENTINEL\n')
    write(project, "src/a.cpp", f'#line 1 "{private}"\n#include "api.hpp"\nint a() {{ return helper(); }}\n')
    write(project, "inc/api.hpp", 'inline int helper() { return 1; }\n')
    commands(project, ["src/a.cpp"])
    db = tmp_path / "index.db"
    index(project, db)
    assert q(db, "search", "helper")["items"][0]["definition_file"] == str(project / "inc/api.hpp")
    assert q(db, "search", "a")["items"][0]["definition_line"] == 3
    assert q(db, "source", private, "--start-line", 1, "--end-line", 1, code=2)["error"]["code"] == "source_not_registered"
    (project / "inc/escape.hpp").symlink_to(private)
    commands(project, ["src/a.cpp"], ["-include", "inc/escape.hpp"])
    assert index(project, db, code=2)["error"]["code"] == "outside_authorized_roots"
    rows = commands(project, ["src/a.cpp"])
    rows[0].update(directory="/", file=str(project / "src/a.cpp"), arguments=["c++", "-c", str(project / "src/a.cpp"), "-I" + str(project / "inc")])
    write(project, "src/a.cpp", f'#include "{private}"\nint a() {{ return 0; }}\n')
    write(project, "compile_commands.json", json.dumps(rows))
    err = index(project, db, code=2)
    assert "OWN_PRIVATE_SENTINEL" not in err["error"]["message"]
    assert private.read_text() == '#error OWN_PRIVATE_SENTINEL\n'
    dep = tmp_path / "dependency"
    write(dep, "dep.hpp", "#define VALUE 7\n")
    write(project, "src/a.cpp", '#include "dep.hpp"\nint a() { return VALUE; }\n')
    commands(project, ["src/a.cpp"], ["-I" + str(dep)])
    cli("index", "--project-root", project, "--dependency-root", dep, "--compilation-database", project, "--database", db, "--analyzer", ANALYZER)
    assert any(f["path"] == str(dep / "dep.hpp") and f["scope"] == "dependency" for f in q(db, "status", "--details", "inputs")["items"])
    assert q(db, "source", dep / "dep.hpp", "--start-line", 1, "--end-line", 1, code=2)["error"]["code"] == "source_not_registered"


@pytest.mark.parametrize("content", ["//" + "x" * 33000 + "\n", "//" + "汉" * 11000 + "\n", "//short\n" * 201])
def test_C_source_budgets(project, tmp_path, content):
    write(project, "a.cpp", content + "int a() { return 0; }\n")
    commands(project, ["a.cpp"])
    db = tmp_path / "index.db"
    index(project, db)
    assert q(db, "source", project / "a.cpp", "--start-line", 1, "--end-line", len(content.splitlines()), "--limit", 1, code=2)["error"]["code"] == "source_limit"


def test_C_missing_invalid_arguments(project, tmp_path):
    write(project, "a.cpp", "int a() { return 0; }\n")
    commands(project, ["a.cpp"])
    db = tmp_path / "index.db"
    index(project, db)
    for cmd in ["show", "callers", "callees", "source"]:
        assert q(db, cmd, code=2)["error"]["code"] == "missing_argument"
    assert q(db, "show", "unknown")["items"] == []
    assert q(db, "status", "--index-id", "unknown", code=2)["error"]["code"] == "index_not_found"
    assert q(db, "search", "--limit", "bad", code=2)["error"]["code"] == "invalid_arguments"
    assert q(db, "source", project / "a.cpp", "--start-line", 0, "--end-line", 1, code=2)["error"]["code"] == "invalid_range"


def synchronized_run(root, db, wrapper, tempdir, mutate, ready_name="ready", release_name="release"):
    tempdir.mkdir()
    argv = [sys.executable, "-m", "codeweave", "index", "--project-root", str(root), "--compilation-database", str(root), "--database", str(db), "--analyzer", str(wrapper), "--toolchain-root", str(ANALYZER.parent), "--toolchain-root", str(wrapper.parent)]
    process = subprocess.Popen(argv, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               env={**os.environ, "PYTHONPATH": str(ROOT / "backend"), "TMPDIR": str(tempdir)})
    try:
        deadline = time.monotonic() + 20
        marker = None
        while time.monotonic() < deadline:
            candidates = list(tempdir.glob("codeweave-index-*/output/" + ready_name))
            if candidates:
                marker = candidates[0]
                break
            if process.poll() is not None:
                raise AssertionError(process.communicate())
            time.sleep(0.01)  # Poll only for the explicit synchronization event.
        assert marker is not None, "analyzer synchronization event missing"
        mutate()
        marker.with_name(release_name).write_text("release")
        out, err = process.communicate(timeout=30)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    if os.environ.get("CODEWEAVE_TEST_LOG"):
        with open(os.environ["CODEWEAVE_TEST_LOG"], "a") as log:
            log.write(json.dumps({"argv": argv, "exit": process.returncode, "stdout": out, "stderr": err, "synchronization": ready_name}) + "\n")
    return process.returncode, json.loads(out or err)


def after_wrapper(tmp_path):
    wrapper = write(tmp_path / "instrumentation", "pause.py", f'''#!/usr/bin/python3
import pathlib, subprocess, sys, time
result=subprocess.run([{str(ANALYZER)!r}, *sys.argv[1:]])
if result.returncode: sys.exit(result.returncode)
folder=pathlib.Path(sys.argv[sys.argv.index('--output')+1]).parent
(folder/'ready').write_text('ready')
for _ in range(3000):
    if (folder/'release').exists(): sys.exit(0)
    time.sleep(0.01)
sys.exit(99)
''')
    wrapper.chmod(0o755)
    return wrapper


@pytest.mark.parametrize("changed", ["cpp", "header", "macro", "ccdb"])
def test_C_after_analysis_snapshots(project, tmp_path, changed):
    write(project, "config.hpp", '#define USE_ALT 0\n')
    header = '''#pragma once
#include "config.hpp"
int old_target(); int new_target();
inline int shared() {
#if USE_ALT
 return new_target();
#else
 return old_target();
#endif
}
'''
    write(project, "shared.hpp", header)
    write(project, "a.cpp", '#include "shared.hpp"\nint old_target() { return 1; }\nint a() { return shared(); }\n')
    write(project, "b.cpp", '#include "shared.hpp"\nint new_target() { return 2; }\nint b() { return shared(); }\n')
    commands(project, ["a.cpp", "b.cpp"])
    original = {p.name: p.read_bytes() for p in project.iterdir() if p.is_file()}
    target = {"cpp": "a.cpp", "header": "shared.hpp", "macro": "config.hpp", "ccdb": "compile_commands.json"}[changed]
    def mutate():
        if changed == "ccdb":
            commands(project, ["a.cpp", "b.cpp"], ["-DUSE_OTHER=1"])
        else:
            (project / target).write_bytes(original[target].replace(b"return 1", b"return 9").replace(b"#define USE_ALT 0", b"#define USE_ALT 1") + b"// changed after AST\n")
    db = tmp_path / "index.db"
    code, result = synchronized_run(project, db, after_wrapper(tmp_path), tmp_path / "stage", mutate)
    assert code == 0, result
    status = q(db, "status")
    assert status["compilation_sha256"] == hashlib.sha256(original["compile_commands.json"]).hexdigest()
    inputs = q(db, "status", "--details", "inputs")["items"]
    for file in ["a.cpp", "b.cpp", "shared.hpp", "config.hpp"]:
        snapshot = q(db, "source", project / file, "--start-line", 1, "--end-line", len(original[file].splitlines()))["content"]
        assert snapshot.encode() == original[file]
        assert next(i["sha256"] for i in inputs if i["path"] == str(project / file)) == hashlib.sha256(original[file]).hexdigest()
    shared = q(db, "search", "shared")["items"][0]
    assert [c["other_name"] for c in q(db, "callees", shared["usr"])["items"]] == ["old_target"]


@pytest.mark.parametrize("changed", ["header", "macro"])
def test_C_cross_tu_inconsistent_rejected(project, tmp_path, changed):
    write(project, "config.hpp", '#define USE_ALT 0\n')
    write(project, "shared.hpp", '''#pragma once
#include "config.hpp"
int old_target(); int new_target();
inline int shared() {
#if USE_ALT
 return new_target();
#else
 return old_target();
#endif
}
''')
    write(project, "a.cpp", '#include "shared.hpp"\nint old_target() { return 1; }\nint a() { return shared(); }\n')
    write(project, "b.cpp", '#include "shared.hpp"\nint new_target() { return 2; }\nint b() { return shared(); }\n')
    commands(project, ["a.cpp", "b.cpp"])
    db = tmp_path / "index.db"
    old = index(project, db)
    instrumentation = tmp_path / "instrumentation"
    instrumentation.mkdir()
    library = instrumentation / "pause_open.so"
    subprocess.run(["cc", "-shared", "-fPIC", str(Path(__file__).parent / "support/pause_open.c"), "-o", str(library), "-ldl"], check=True, capture_output=True)
    wrapper = write(instrumentation, "pause.py", f'''#!/usr/bin/python3
import os, pathlib, sys
folder=pathlib.Path(sys.argv[sys.argv.index('--output')+1]).parent
os.environ.update(LD_PRELOAD={str(library)!r}, AUDIT_FIXTURE={str(project)!r}, AUDIT_PAUSE_DIR=str(folder))
os.execv({str(ANALYZER)!r}, [{str(ANALYZER)!r}, *sys.argv[1:]])
''')
    wrapper.chmod(0o755)
    target = project / ("shared.hpp" if changed == "header" else "config.hpp")
    old_text = target.read_text()
    def mutate():
        target.write_text(old_text.replace("return old_target()", "return new_target()") if changed == "header" else "#define USE_ALT 1\n")
    code, result = synchronized_run(project, db, wrapper, tmp_path / "stage", mutate, "second-source-ready", "second-source-release")
    assert code == 2 and result["error"]["code"] == "inconsistent_input", result
    assert q(db, "status")["index_id"] == old["index_id"]
    assert q(db, "source", target, "--start-line", 1, "--end-line", len(old_text.splitlines()))["content"] == old_text


def test_C_real_isolation_boundary(project, tmp_path):
    """Trusted probe observes the actual public-entry sandbox, never touches real secrets."""
    source = write(project, "a.cpp", "int a() { return 1; }\n")
    sentinel = write(tmp_path, "sentinel", "KEEP")
    commands(project, ["a.cpp"])
    probe = write(tmp_path / "instrumentation", "probe.py", f'''#!/usr/bin/python3
import errno, json, os, pathlib, sys
folder=pathlib.Path(sys.argv[sys.argv.index('--output')+1]).parent
observed={{}}
for label,path in [('source',{str(source)!r}),('outside',{str(sentinel)!r}),('proc_escape',{('/proc/'+str(os.getpid())+'/root'+str(sentinel))!r})]:
    try:
        fd=os.open(path,os.O_WRONLY|os.O_APPEND)
        os.close(fd)
        observed[label]='unexpected writable access'
    except OSError as error: observed[label]=error.errno
(folder/'probe.json').write_text(json.dumps(observed))
print(json.dumps(observed),file=sys.stderr)
sys.exit(19)
''')
    probe.chmod(0o755)
    error = cli("index", "--project-root", project, "--compilation-database", project, "--database", tmp_path / "index.db", "--analyzer", probe, code=2)
    observed = json.loads(error["error"]["message"])
    assert observed["source"] == 30  # EROFS
    assert observed["outside"] == 2 and observed["proc_escape"] == 2
    assert sentinel.read_text() == "KEEP" and source.read_text() == "int a() { return 1; }\n"


def test_C_definition_order_and_conflict(project, tmp_path):
    write(project, "a.cpp", "int later(); int invoke() { return later(); }\n")
    write(project, "b.cpp", "int later() { return 1; }\n")
    commands(project, ["a.cpp", "b.cpp"])
    db = tmp_path / "index.db"
    index(project, db)
    first = q(db, "search", "later")["items"][0]
    assert first["definition_file"] == str(project / "b.cpp")
    write(project, "a.cpp", "int later() { return 1; }\n")
    write(project, "b.cpp", "int later(); int invoke() { return later(); }\n")
    commands(project, ["b.cpp", "a.cpp"])
    index(project, db)
    second = q(db, "search", "later")["items"][0]
    assert second["usr"] == first["usr"] and second["definition_file"] == str(project / "a.cpp")
    write(project, "b.cpp", "int later() { return 2; }\n")
    assert index(project, db, code=2)["error"]["code"] == "conflicting_definition"


def test_C_conflicting_inline_macro_definitions(project, tmp_path):
    write(project, "header.hpp", "inline int value() { return NUMBER; }\n")
    write(project, "a.cpp", '#include "header.hpp"\nint a() { return value(); }\n')
    write(project, "b.cpp", '#include "header.hpp"\nint b() { return value(); }\n')
    rows = commands(project, ["a.cpp", "b.cpp"], ["-DNUMBER=1"])
    rows[1]["arguments"][3] = "-DNUMBER=2"
    write(project, "compile_commands.json", json.dumps(rows))
    assert index(project, tmp_path / "index.db", code=2)["error"]["code"] == "conflicting_definition"


def test_C_standard_headers(project, tmp_path):
    write(project, "a.cpp", '#include <stddef.h>\n#include <stdint.h>\n#include <vector>\nint a() { std::vector<int> v; return sizeof(int32_t)+v.size(); }\n')
    commands(project, ["a.cpp"])
    db = tmp_path / "index.db"
    index(project, db)
    status = q(db, "status")
    assert any(i["scope"] == "dependency" for i in q(db, "status", "--details", "inputs")["items"])
    assert all(u["command"]["clang_version"] for u in q(db, "status", "--details", "units")["items"])


def test_A_command_directory_in_identity(project):
    from codeweave.index import _load_compilation_database
    write(project, "a.cpp", "int a();\n")
    for name in ["one/inc", "two/inc"]:
        (project / name).mkdir(parents=True)
    rows = [{"directory": str(project / d), "file": "../a.cpp", "arguments": ["c++", "-Iinc", "-c", "../a.cpp"]} for d in ["one", "two"]]
    write(project, "compile_commands.json", json.dumps(rows))
    with pytest.raises(IndexError, match="one compilation configuration"):
        _load_compilation_database(project, project, [project], {})


def test_A_missing_sandbox_is_closed(project, monkeypatch):
    from codeweave.index import _sandbox_command
    monkeypatch.setenv("PATH", "")
    with pytest.raises(IndexError, match="no unisolated fallback"):
        _sandbox_command(ANALYZER, project, project / "facts", project, [project], set())


def raw_analyzer(root, facts):
    argv = [str(ANALYZER), "--compilation-database", str(root), "--project-root", str(root), "--output", str(facts)]
    p = subprocess.run(argv, capture_output=True, text=True, timeout=40)
    if os.environ.get("CODEWEAVE_TEST_LOG"):
        with open(os.environ["CODEWEAVE_TEST_LOG"], "a") as log:
            log.write(json.dumps({"level": "B trusted owned input, direct real analyzer", "argv": argv, "exit": p.returncode, "stdout": p.stdout, "stderr": p.stderr, "facts_sha256": sha(facts) if facts.exists() else None}) + "\n")
    return p, [json.loads(line) for line in facts.read_text().splitlines()]


def test_B_real_fact_buffers_and_macro_instances(project, tmp_path):
    semantic_fixture(project)
    p, facts = raw_analyzer(project, tmp_path / "facts.jsonl")
    assert p.returncode == 0, p.stderr
    files = [r for r in facts if r["type"] == "file"]
    for r in files:
        assert bytes.fromhex(r["content_hex"]) == Path(r["path"]).read_bytes()
        assert r["sha256"] == sha(Path(r["path"]))
    macros = next(r for r in facts if r["type"] == "entity" and r["name"] == "macros")
    calls = [r for r in facts if r["type"] == "call" and r["caller_usr"] == macros["usr"]]
    assert len(calls) == 6
    assert all(r["location"]["line"] == 7 and r["spelling"]["line"] in {2, 3} and r["expansion_chain"] for r in calls)
    assert len([r for r in facts if r["type"] == "unit" and r["status"] == "complete"]) == 2


def test_B_driver_error_is_failure(project, tmp_path):
    write(project, "a.cpp", "int a() { return 1; }\n")
    commands(project, ["a.cpp"], ["-mnotarealclangoption"])
    p, facts = raw_analyzer(project, tmp_path / "facts.jsonl")
    assert p.returncode != 0
    units = [r for r in facts if r["type"] == "unit"]
    assert units[0]["status"] == "failed"
    assert any(d["level"] == "error" for d in units[0]["diagnostics"])


def test_B_C_shared_inline_lambda_identity(project, tmp_path):
    write(project, "header.hpp", 'int leaf(); inline int shared() { auto f=[] { return leaf(); }; return f(); }\n')
    write(project, "a.cpp", '#include "header.hpp"\nint a() { return shared(); }\n')
    write(project, "b.cpp", '#include "header.hpp"\nint leaf() { return 1; }\nint b() { return shared(); }\n')
    commands(project, ["a.cpp", "b.cpp"])
    db = tmp_path / "index.db"
    index(project, db)
    shared = q(db, "search", "shared")["items"]
    entry = next(s for s in shared if s["name"] == "shared")
    calls = q(db, "callees", entry["usr"])["items"]
    assert len(calls) == 1 and len(calls[0]["units"]) == 2
    inner = q(db, "callees", calls[0]["target_usr"])["items"]
    assert len(inner) == 1 and inner[0]["other_name"] == "leaf" and len(inner[0]["units"]) == 2
