"""Versioned index. Untrusted compilation input runs only inside Bubblewrap."""
from __future__ import annotations
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import sqlite3
import subprocess
import tempfile
import uuid

SCHEMA_VERSION = 3
FACT_SCHEMA_VERSION = 2
MAX_STATUS_BYTES = 32_768  # Serialized UTF-8 JSON, using the CLI serializer.
MAX_STATUS_FIELD_BYTES = 2_048
STATUS_CHUNK_BYTES = 4_096
MAX_SOURCE_LINES = 200
MAX_SOURCE_BYTES = 32_768  # Raw snapshot bytes, before JSON escaping.
COVERAGE_LIMITS = ["explicit evaluated CallExpr only", "no implicit lifecycle or global initializer edges",
                   "virtual dispatch is static, indirect targets unresolved", "no PCH/modules",
                   "system inputs are hashed, not archived; full build reproducibility is not claimed"]

class IndexError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code

def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))

def _resolve(path: Path) -> Path:
    try:
        return path.expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise IndexError("invalid_path", str(error)) from error

def _inside(path: Path, root: Path) -> bool:
    return path.is_relative_to(root)

SCHEMA = """
CREATE TABLE indexes (
 id TEXT PRIMARY KEY, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 compilation_database TEXT NOT NULL, compilation_sha256 TEXT NOT NULL,
 project_root TEXT NOT NULL, coverage TEXT NOT NULL, diagnostic TEXT NOT NULL,
 schema_version INTEGER NOT NULL, toolchain TEXT NOT NULL);
CREATE TABLE files (
 index_id TEXT NOT NULL REFERENCES indexes(id), path TEXT NOT NULL, content BLOB,
 sha256 TEXT NOT NULL, scope TEXT NOT NULL, PRIMARY KEY(index_id,path));
CREATE TABLE units (
 index_id TEXT NOT NULL REFERENCES indexes(id), source_file TEXT NOT NULL,
 command_sha256 TEXT NOT NULL, status TEXT NOT NULL, diagnostics TEXT NOT NULL,
 command TEXT NOT NULL, PRIMARY KEY(index_id,source_file));
CREATE TABLE unit_files (
 index_id TEXT NOT NULL, source_file TEXT NOT NULL, path TEXT NOT NULL,
 PRIMARY KEY(index_id,source_file,path),
 FOREIGN KEY(index_id,source_file) REFERENCES units(index_id,source_file),
 FOREIGN KEY(index_id,path) REFERENCES files(index_id,path));
CREATE TABLE symbols (
 index_id TEXT NOT NULL REFERENCES indexes(id), usr TEXT NOT NULL, name TEXT NOT NULL,
 kind TEXT NOT NULL, declaration_file TEXT NOT NULL, declaration_line INTEGER NOT NULL,
 declaration_column INTEGER NOT NULL, definition_file TEXT, definition_line INTEGER,
 definition_column INTEGER, anchors TEXT NOT NULL, PRIMARY KEY(index_id,usr),
 FOREIGN KEY(index_id,declaration_file) REFERENCES files(index_id,path),
 FOREIGN KEY(index_id,definition_file) REFERENCES files(index_id,path));
CREATE TABLE symbol_units (
 index_id TEXT NOT NULL, usr TEXT NOT NULL, source_file TEXT NOT NULL,
 PRIMARY KEY(index_id,usr,source_file),
 FOREIGN KEY(index_id,usr) REFERENCES symbols(index_id,usr),
 FOREIGN KEY(index_id,source_file) REFERENCES units(index_id,source_file));
CREATE TABLE calls (
 id INTEGER PRIMARY KEY, index_id TEXT NOT NULL REFERENCES indexes(id), instance_id TEXT NOT NULL,
 caller_usr TEXT NOT NULL, target_usr TEXT, dispatch TEXT NOT NULL,
 file TEXT NOT NULL, line INTEGER NOT NULL, column_number INTEGER NOT NULL, anchors TEXT NOT NULL,
 UNIQUE(index_id,instance_id), FOREIGN KEY(index_id,caller_usr) REFERENCES symbols(index_id,usr),
 FOREIGN KEY(index_id,file) REFERENCES files(index_id,path));
CREATE TABLE call_units (
 index_id TEXT NOT NULL, instance_id TEXT NOT NULL, source_file TEXT NOT NULL,
 PRIMARY KEY(index_id,instance_id,source_file),
 FOREIGN KEY(index_id,instance_id) REFERENCES calls(index_id,instance_id),
 FOREIGN KEY(index_id,source_file) REFERENCES units(index_id,source_file));
CREATE INDEX calls_by_caller ON calls(index_id,caller_usr);
CREATE INDEX calls_by_target ON calls(index_id,target_usr);
PRAGMA user_version = 3;
"""

def _connection(database: Path, readonly: bool = False) -> sqlite3.Connection:
    try:
        connection = sqlite3.connect(database.absolute().as_uri() + ("?mode=ro" if readonly else "?mode=rw"), uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection
    except sqlite3.Error as error:
        raise IndexError("database_error", "cannot open index database") from error

def _schema_identity(connection: sqlite3.Connection) -> list:
    return [tuple(row) for row in connection.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name")]

def _validate_schema(connection: sqlite3.Connection) -> None:
    expected = sqlite3.connect(":memory:")
    try:
        expected.executescript(SCHEMA)
        if connection.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION or _schema_identity(connection) != _schema_identity(expected):
            raise IndexError("schema_incompatible", "database schema is incompatible; rebuild into a new database path")
    finally:
        expected.close()

def _arguments(entry: dict) -> list[str]:
    args = entry.get("arguments")
    if isinstance(args, list) and args and all(isinstance(x, str) for x in args):
        return args
    if isinstance(entry.get("command"), str):
        return shlex.split(entry["command"])
    raise IndexError("invalid_compilation_database", "compile command lacks arguments")

def _trusted_root(path: Path) -> Path:
    root = _resolve(path)
    if not root.is_dir() or root in {Path("/"), Path("/home"), Path.home(), Path("/tmp")}:
        raise IndexError("invalid_project_root", "authorize a specific project/dependency directory, not a host-wide root")
    return root

def _safe_arguments(arguments: list[str], source: Path, directory: Path, roots: list[Path]) -> list[str]:
    # LLVM expands leading-@ tokens before interpreting option/value groups.
    # Check the entire original argv, including values and discarded -o operands.
    if any(arg.startswith("@") for arg in arguments):
        raise IndexError("unsafe_compilation_argument", "response files are not supported in any argument position")
    if not arguments or Path(arguments[0]).name not in {"c++", "g++", "clang++", "clang"}:
        raise IndexError("unsafe_compilation_argument", "compiler executable is not supported")
    # The compiler spelling selects a driver language, never an executable/mount.
    safe = ["/usr/bin/clang++" if "++" in arguments[0] else "/usr/bin/clang"]
    paths = {"-I", "-isystem", "-iquote", "-include", "-imacros"}
    values = {"-D", "-U", "-std", "-x", "-target", "--target"}
    def authorized(value: str) -> str:
        candidate = Path(value)
        physical = (candidate if candidate.is_absolute() else directory / candidate).resolve()
        if not any(_inside(physical, root) for root in roots):
            raise IndexError("outside_authorized_roots", "compiler path is outside authorized roots")
        return str(physical)
    i = 1
    while i < len(arguments):
        arg = arguments[i]
        if arg == "-o":
            if i + 1 >= len(arguments):
                raise IndexError("invalid_compilation_database", "output flag lacks value")
            i += 2
            continue
        if arg == "-c":
            i += 1
            continue
        if arg in paths | values:
            if i + 1 >= len(arguments):
                raise IndexError("invalid_compilation_database", "compiler flag lacks value")
            val = arguments[i + 1]
            safe.extend([arg, authorized(val) if arg in paths else val])
            i += 2
            continue
        joined = next((flag for flag in sorted(paths, key=len, reverse=True) if arg.startswith(flag) and arg != flag), None)
        if joined:
            safe.extend([joined, authorized(arg[len(joined):])])
        elif arg.startswith(("-D", "-U", "-std=", "--target=")):
            safe.append(arg)
        elif re.fullmatch(r"-O[0-3sgz]?|-W(?:no-)?[a-zA-Z0-9_-]+(?:=[a-zA-Z0-9_-]+)?|-m[a-zA-Z0-9_=+.-]+", arg):
            safe.append(arg)
        elif arg in {"-g", "-pthread", "-fPIC", "-fPIE", "-pipe", "-fno-exceptions", "-fno-rtti", "-fexceptions", "-frtti"}:
            safe.append(arg)
        elif not arg.startswith(("-", "@")) and (directory / arg).resolve() == source:
            pass
        else:
            raise IndexError("unsafe_compilation_argument", f"rejected compiler argument: {arg}")
        i += 1
    return safe + ["-c", str(source)]

def _load_compilation_database(directory: Path, project_root: Path, roots: list[Path], toolchain: dict) -> tuple[list[dict], bytes]:
    directory = _resolve(directory)
    try:
        raw = (directory / "compile_commands.json").read_bytes()
        entries = json.loads(raw)
    except (OSError, ValueError) as error:
        raise IndexError("invalid_compilation_database", "cannot read UTF-8 compile_commands.json") from error
    if not isinstance(entries, list) or not entries:
        raise IndexError("invalid_compilation_database", "compilation database has no commands")
    commands = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("file"), str) or not isinstance(entry.get("directory"), str):
            raise IndexError("invalid_compilation_database", "command requires file and directory strings")
        cwd = Path(entry["directory"])
        if not cwd.is_absolute():
            raise IndexError("invalid_compilation_database", "command directory must be absolute")
        cwd = _resolve(cwd)
        source = _resolve(cwd / entry["file"])
        if not source.is_file() or not _inside(source, project_root):
            raise IndexError("outside_project_root", "translation unit is outside authorized project root")
        args = _safe_arguments(_arguments(entry), source, cwd, roots)
        command = {"directory": str(cwd), "file": str(source), "arguments": args}
        digest = _sha256(_json({**command, "toolchain": toolchain}).encode())
        if str(source) in commands and commands[str(source)]["command_sha256"] != digest:
            raise IndexError("multiple_configurations", "select one compilation configuration per translation unit")
        commands[str(source)] = {**command, "command_sha256": digest}
    return list(commands.values()), raw

def _sandbox_command(analyzer: Path, database_directory: Path, facts: Path, project_root: Path, roots: list[Path], directories: set[Path]) -> list[str]:
    bwrap = shutil.which("bwrap")
    if not bwrap:
        raise IndexError("sandbox_unavailable", "bubblewrap is required; no unisolated fallback")
    args = [bwrap, "--die-with-parent", "--unshare-all", "--new-session", "--clearenv",
            "--setenv", "PATH", "/usr/bin:/bin", "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]
    for root in dict.fromkeys([Path("/usr"), Path("/lib"), Path("/lib64"), *roots, database_directory]):
        if root.exists():
            args += ["--ro-bind", str(root), str(root)]
    args += ["--ro-bind", str(analyzer), str(analyzer)]
    # cwd affects option resolution only. Empty directories confer no host access.
    for cwd in sorted(directories):
        args += ["--dir", str(cwd)]
    facts.parent.mkdir(parents=True, exist_ok=True)
    args += ["--bind", str(facts.parent), str(facts.parent), "--chdir", str(project_root)]
    return args + [str(analyzer), "--compilation-database", str(database_directory), "--output", str(facts), "--project-root", str(project_root)]

def _collect(records: list, commands: list[dict], project_root: Path) -> tuple:
    tus = {c["file"] for c in commands}
    files, units, merged, calls = {}, {}, {}, {}
    file_units, symbol_units, call_units = set(), set(), set()
    for r in records:
        if not isinstance(r, dict) or r.get("schema") != FACT_SCHEMA_VERSION or r.get("tu") not in tus or r.get("type") not in {"file", "unit", "entity", "call"}:
            raise IndexError("invalid_facts", "unsupported fact version, type or translation unit")
        if r["type"] == "file":
            path, digest = r["path"], r["sha256"]
            if not Path(path).is_absolute() or str(Path(path)) != path or ".." in Path(path).parts:
                raise IndexError("invalid_facts", "noncanonical physical path")
            project = _inside(Path(path), project_root)
            content = bytes.fromhex(r["content_hex"]) if r["content_hex"] is not None else None
            if not re.fullmatch('[0-9a-f]{64}', digest) or (content is not None and _sha256(content) != digest) or project != (content is not None):
                raise IndexError("invalid_facts", "invalid buffer identity or scope")
            value = (content, digest, "project" if project else "dependency")
            if path in files and files[path] != value:
                raise IndexError("inconsistent_input", "translation units observed different versions of one input")
            files[path] = value
            file_units.add((r["tu"], path))
        elif r["type"] == "unit":
            if r["tu"] in units or r.get("status") != "complete" or not isinstance(r.get("diagnostics"), list) or any(not isinstance(d, dict) or d.get("level") not in {"note", "warning"} or not isinstance(d.get("message"), str) for d in r["diagnostics"]):
                raise IndexError("analysis_failed", "unit failed or has invalid diagnostics")
            units[r["tu"]] = r
    if set(units) != tus or not all((tu, tu) in file_units for tu in tus):
        raise IndexError("incomplete_analysis", "missing unit results or source buffers")
    def anchor(loc: dict, tu: str) -> None:
        if not isinstance(loc, dict) or loc.get("file") not in files or (tu, loc["file"]) not in file_units:
            raise IndexError("invalid_facts", "anchor lacks a unit buffer")
        content, digest, _ = files[loc["file"]]
        if loc.get("sha256") != digest or not all(type(loc.get(k)) is int and loc[k] >= (0 if k == "offset" else 1) for k in ("line", "column", "offset")):
            raise IndexError("invalid_facts", "anchor identity is invalid")
        if content is not None:
            offset = loc["offset"]
            prefix = content[:offset]
            if offset > len(content) or prefix.count(b"\n") + 1 != loc["line"] or len(prefix.rsplit(b"\n", 1)[-1]) + 1 != loc["column"]:
                raise IndexError("invalid_facts", "anchor disagrees with source bytes")
    for r in records:
        if r["type"] == "entity":
            if not all(isinstance(r.get(k), str) and r[k] for k in ("usr", "name", "kind")):
                raise IndexError("invalid_facts", "invalid entity")
            anchor(r["declaration"], r["tu"])
            if r.get("definition"):
                anchor(r["definition"], r["tu"])
            prior = merged.get(r["usr"])
            if prior and prior.get("definition") and r.get("definition") and (prior["definition"] != r["definition"] or prior.get("definition_odr") != r.get("definition_odr")):
                raise IndexError("conflicting_definition", "incompatible definitions for one entity")
            if not prior or (not prior.get("definition") and r.get("definition")):
                merged[r["usr"]] = r
            symbol_units.add((r["usr"], r["tu"]))
        elif r["type"] == "call":
            anchor(r["location"], r["tu"])
            anchor(r["spelling"], r["tu"])
            for pair in r["expansion_chain"]:
                for loc in pair:
                    anchor(loc, r["tu"])
            if r.get("dispatch") not in {"direct", "virtual", "unknown-function-pointer", "unknown-dependent"} or not isinstance(r.get("caller_usr"), str) or not (r.get("target_usr") is None or isinstance(r["target_usr"], str)):
                raise IndexError("invalid_facts", "invalid call")
            identity = {key: r[key] for key in ("caller_usr", "target_usr", "dispatch", "location", "spelling", "expansion_chain")}
            key = _sha256(_json(identity).encode())
            calls[key] = r
            call_units.add((key, r["tu"]))
    if not merged:
        raise IndexError("empty_analysis", "analysis produced no entities")
    if any(c["caller_usr"] not in merged for c in calls.values()):
        raise IndexError("invalid_facts", "call has no caller entity")
    return files, units, merged, calls, file_units, symbol_units, call_units

def build_index(database: Path, compilation_database: Path, analyzer: Path, project_root: Path,
                dependency_roots: list[Path] | None = None, toolchain_roots: list[Path] | None = None, resource_directory: Path | None = None) -> dict:
    if database.exists():
        existing = _connection(database, readonly=True)
        try:
            _validate_schema(existing)
        except sqlite3.Error as error:
            raise IndexError("database_error", "cannot validate index database") from error
        finally:
            existing.close()
    project_root = _trusted_root(project_root)
    deps = [_trusted_root(p) for p in dependency_roots or []]
    tool_roots = [_trusted_root(p) for p in toolchain_roots or []]
    resource = _trusted_root(resource_directory) if resource_directory else None
    if resource:
        tool_roots.append(resource)
    analyzer = _resolve(analyzer)
    if not analyzer.is_file() or not os.access(analyzer, os.X_OK):
        raise IndexError("analyzer_unavailable", "analyzer is not executable")
    toolchain = {"analyzer_sha256": _sha256(analyzer.read_bytes()), "protocol": FACT_SCHEMA_VERSION,
                 "toolchain_roots": list(map(str, tool_roots)), "dependency_roots": list(map(str, deps)),
                 "system_roots": ["/usr", "/lib", "/lib64"]}
    commands, raw = _load_compilation_database(compilation_database, project_root, [project_root, *deps, *tool_roots, Path("/usr")], toolchain)
    if resource:
        for command in commands:
            command["arguments"] += ["-resource-dir", str(resource)]
            command["command_sha256"] = _sha256(_json({k: v for k, v in command.items() if k != "command_sha256"}).encode() + _json(toolchain).encode())
    with tempfile.TemporaryDirectory(prefix="codeweave-index-") as temp:
        stage = Path(temp)
        ccdb = stage / "compilation"
        ccdb.mkdir()
        (ccdb / "compile_commands.json").write_text(_json([{k: c[k] for k in ("directory", "file", "arguments")} for c in commands]))
        facts = stage / "output" / "facts.jsonl"
        result = subprocess.run(_sandbox_command(analyzer, ccdb, facts, project_root, [project_root, *deps, *tool_roots], {Path(c["directory"]) for c in commands}), text=True, capture_output=True, check=False)
        if result.returncode:
            raise IndexError("analysis_failed", result.stderr.strip() or "isolated analyzer failed")
        try:
            records = [json.loads(line) for line in facts.read_text().splitlines() if line]
            collected = _collect(records, commands, project_root)
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise IndexError("invalid_facts", "analyzer returned malformed facts") from error
    files, units, merged, calls, file_units, symbol_units, call_units = collected
    index_id = str(uuid.uuid4())
    database.parent.mkdir(parents=True, exist_ok=True)
    new = not database.exists()
    temporary = None
    connection = None
    try:
        if new:
            fd, path = tempfile.mkstemp(prefix=".codeweave-", dir=database.parent)
            os.close(fd)
            temporary = Path(path)
            connection = _connection(temporary)
            connection.executescript(SCHEMA)
        else:
            connection = _connection(database)
            _validate_schema(connection)
        with connection:
            connection.execute("INSERT INTO indexes VALUES (?,CURRENT_TIMESTAMP,?,?,?,?,?,?,?)", (index_id, str(_resolve(compilation_database)), _sha256(raw), str(project_root), "units_complete", result.stderr, SCHEMA_VERSION, _json(toolchain)))
            connection.executemany("INSERT INTO files VALUES (?,?,?,?,?)", [(index_id, path, *value) for path, value in files.items()])
            connection.executemany("INSERT INTO units VALUES (?,?,?,?,?,?)", [(index_id, c["file"], c["command_sha256"], units[c["file"]]["status"], _json(units[c["file"]]["diagnostics"]), _json({**c, "clang_version": units[c["file"]].get("clang_version")})) for c in commands])
            connection.executemany("INSERT INTO unit_files VALUES (?,?,?)", [(index_id, *row) for row in sorted(file_units)])
            for r in merged.values():
                d, f = r["declaration"], r.get("definition")
                connection.execute("INSERT INTO symbols VALUES (?,?,?,?,?,?,?,?,?,?,?)", (index_id, r["usr"], r["name"], r["kind"], d["file"], d["line"], d["column"], *(f[k] if f else None for k in ("file", "line", "column")), _json({"declaration": d, "definition": f, "definition_odr": r.get("definition_odr")})))
            connection.executemany("INSERT INTO symbol_units VALUES (?,?,?)", [(index_id, *row) for row in sorted(symbol_units)])
            for key, r in calls.items():
                loc = r["location"]
                connection.execute("INSERT INTO calls(index_id,instance_id,caller_usr,target_usr,dispatch,file,line,column_number,anchors) VALUES (?,?,?,?,?,?,?,?,?)", (index_id, key, r["caller_usr"], r["target_usr"], r["dispatch"], loc["file"], loc["line"], loc["column"], _json({k: r[k] for k in ("location", "spelling", "expansion_chain")})))
            connection.executemany("INSERT INTO call_units VALUES (?,?,?)", [(index_id, *row) for row in sorted(call_units)])
        connection.close()
        connection = None
        if temporary:
            os.link(temporary, database)  # Atomic publication without overwriting an existing path.
    except sqlite3.Error as error:
        raise IndexError("database_error", "database transaction failed") from error
    finally:
        if connection:
            connection.close()
        if temporary:
            temporary.unlink(missing_ok=True)
    return {"index_id": index_id, "coverage": "units_complete", "entities": len(merged), "calls": len(calls), "call_observations": sum(r["type"] == "call" for r in records), "units": len(units)}

def _index(connection: sqlite3.Connection, index_id: str | None) -> str:
    row = connection.execute("SELECT id FROM indexes WHERE id = ?", (index_id,)).fetchone() if index_id else connection.execute("SELECT id FROM indexes ORDER BY rowid DESC LIMIT 1").fetchone()
    if not row:
        raise IndexError("index_not_found", "no matching index")
    return str(row["id"])


def _status_size(value: object) -> int:
    # Match cli._json, including its trailing newline, rather than counting raw text.
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")) + 1


def _status_value(value: str, parse_json: bool = False) -> object:
    raw = value.encode("utf-8")
    if len(raw) <= MAX_STATUS_FIELD_BYTES:
        return json.loads(value) if parse_json else value
    return {"truncated": True, "preview": raw[:MAX_STATUS_FIELD_BYTES].decode("utf-8", errors="ignore"),
            "utf8_bytes": len(raw), "sha256": _sha256(raw)}


def _status(connection: sqlite3.Connection, active: str, limit: int, offset: int,
            details: str | None, field: str | None, field_offset: int) -> dict:
    if details not in {None, "units", "inputs"}:
        raise IndexError("invalid_arguments", "status details must be units or inputs")
    if field:
        allowed = {None: {"toolchain"}, "units": {"command", "diagnostics", "source_file"}, "inputs": {"path"}}
        if field not in allowed[details] or (details and limit != 1):
            raise IndexError("invalid_arguments", "field must match status details; select one detail with --limit 1")
        if details:
            table, order = ("units", "source_file") if details == "units" else ("files", "path")
            row = connection.execute(f"SELECT {field} FROM {table} WHERE index_id=? ORDER BY {order} LIMIT 1 OFFSET ?", (active, offset)).fetchone()
        else:
            if offset:
                raise IndexError("invalid_arguments", "toolchain field does not have a row offset")
            row = connection.execute("SELECT toolchain FROM indexes WHERE id=?", (active,)).fetchone()
        if row is None:
            raise IndexError("status_item_not_found", "no status item at the requested offset")
        raw = row[0].encode("utf-8")
        if field_offset > len(raw):
            raise IndexError("invalid_offset", "field offset exceeds the stored UTF-8 bytes")
        chunk = raw[field_offset:field_offset + STATUS_CHUNK_BYTES]
        end = field_offset + len(chunk)
        response = {"index_id": active, "details": details, "offset": offset, "field": field,
                    "field_offset": field_offset, "field_bytes": len(raw), "field_sha256": _sha256(raw),
                    "encoding": "base64", "content": base64.b64encode(chunk).decode("ascii"),
                    "returned_bytes": len(chunk), "next_field_offset": end if end < len(raw) else None,
                    "has_more": end < len(raw)}
    elif details:
        table, order = ("units", "source_file") if details == "units" else ("files", "path")
        columns = "source_file,command_sha256,status,diagnostics,command" if details == "units" else "path,sha256,scope"
        total = connection.execute(f"SELECT count(*) FROM {table} WHERE index_id=?", (active,)).fetchone()[0]
        rows = connection.execute(f"SELECT {columns} FROM {table} WHERE index_id=? ORDER BY {order} LIMIT ? OFFSET ?", (active, limit, offset))
        response = {"index_id": active, "details": details, "offset": offset, "limit": limit,
                    "total_count": total, "items": [], "returned_count": 0, "remaining_count": max(0, total - offset),
                    "next_offset": None, "has_more": False, "budget_limited": False}
        for row in rows:
            item = {key: _status_value(row[key], key in {"command", "diagnostics"}) for key in row.keys()}
            response["items"].append(item)
            count = len(response["items"])
            remaining = max(0, total - offset - count)
            response.update(returned_count=count, remaining_count=remaining, next_offset=offset + count if remaining else None, has_more=bool(remaining))
            if _status_size(response) > MAX_STATUS_BYTES - 128:
                response["items"].pop()
                response["budget_limited"] = True
                break
        count = len(response["items"])
        remaining = max(0, total - offset - count)
        response.update(returned_count=count, remaining_count=remaining, next_offset=offset + count if remaining else None, has_more=bool(remaining))
        if not count and remaining:
            raise IndexError("status_limit", "one status item exceeds the response budget; use field chunks")
    else:
        if offset or field_offset:
            raise IndexError("invalid_arguments", "offset requires status details or a field")
        row = connection.execute("SELECT * FROM indexes WHERE id=?", (active,)).fetchone()
        diagnostic_summary = {"error": 0, "warning": 0, "note": 0}
        for level, count in connection.execute("SELECT json_extract(d.value,'$.level'),count(*) FROM units u,json_each(u.diagnostics) d WHERE u.index_id=? GROUP BY json_extract(d.value,'$.level')", (active,)):
            diagnostic_summary[level] = count
        response = {"index_id": active, "schema_version": row["schema_version"], "coverage": _status_value(row["coverage"]),
                    "project_root": _status_value(row["project_root"]), "compilation_database": _status_value(row["compilation_database"]),
                    "compilation_sha256": _status_value(row["compilation_sha256"]),
                    "unit_count": connection.execute("SELECT count(*) FROM units WHERE index_id=?", (active,)).fetchone()[0],
                    "input_count": connection.execute("SELECT count(*) FROM files WHERE index_id=?", (active,)).fetchone()[0],
                    "diagnostic_summary": diagnostic_summary, "limitations": COVERAGE_LIMITS}
    if _status_size(response) > MAX_STATUS_BYTES:
        raise IndexError("status_limit", "status response exceeds the serialized JSON budget")
    return response


def query(database: Path, command: str, value: str | None = None, index_id: str | None = None, limit: int = 50, start_line: int | None = None, end_line: int | None = None, details: str | None = None, offset: int = 0, field: str | None = None, field_offset: int = 0) -> dict[str, object]:
    if not 1 <= limit <= 200:
        raise IndexError("invalid_limit", "limit must be from 1 to 200")
    if not 0 <= offset <= 2**63 - 1 or field_offset < 0:
        raise IndexError("invalid_offset", "row offset must fit a nonnegative SQLite integer; field offset must be nonnegative")
    if command != "status" and (details is not None or offset or field is not None or field_offset):
        raise IndexError("invalid_arguments", "details, offsets and field apply only to status")
    if field_offset and not field:
        raise IndexError("invalid_arguments", "field offset requires a field")
    if command in {"show", "callers", "callees", "source"} and not value:
        raise IndexError("missing_argument", f"{command} requires a value")
    if not database.is_file():
        raise IndexError("database_not_found", "index database does not exist")
    connection = _connection(database, readonly=True)
    try:
        _validate_schema(connection)
        active = _index(connection, index_id)
        if command == "status":
            return _status(connection, active, limit, offset, details, field, field_offset)
        if command in {"show", "callers", "callees", "source"} and not value:
            raise IndexError("missing_argument", f"{command} requires a value")
        if command == "search":
            rows = connection.execute("SELECT * FROM symbols WHERE index_id = ? AND name LIKE ? ORDER BY name, usr LIMIT ?", (active, f"%{value or ''}%", limit + 1)).fetchall()
        elif command == "show":
            rows = connection.execute("SELECT * FROM symbols WHERE index_id = ? AND usr = ?", (active, value)).fetchall()
        elif command in {"callers", "callees"}:
            other, filter_field = ("caller_usr", "target_usr") if command == "callers" else ("target_usr", "caller_usr")
            rows = connection.execute(f"SELECT calls.*, symbols.name AS other_name FROM calls LEFT JOIN symbols ON symbols.index_id = calls.index_id AND symbols.usr = calls.{other} WHERE calls.index_id = ? AND calls.{filter_field} = ? ORDER BY calls.file, calls.line, calls.column_number LIMIT ?", (active, value, limit + 1)).fetchall()
        elif command == "source":
            if start_line is None or end_line is None or start_line < 1 or end_line < start_line:
                raise IndexError("invalid_range", "source requires a valid inclusive line range")
            if end_line - start_line + 1 > MAX_SOURCE_LINES:
                raise IndexError("source_limit", f"source range exceeds {MAX_SOURCE_LINES} lines")
            row = connection.execute("SELECT content FROM files WHERE index_id = ? AND path = ?", (active, value)).fetchone()
            if not row or row["content"] is None:
                raise IndexError("source_not_registered", "path is not registered in this index")
            lines = bytes(row["content"]).splitlines(keepends=True)
            if end_line > len(lines):
                raise IndexError("invalid_range", "source range exceeds the indexed snapshot")
            content = b"".join(lines[start_line - 1:end_line])
            if len(content) > MAX_SOURCE_BYTES:
                raise IndexError("source_limit", f"source range exceeds {MAX_SOURCE_BYTES} bytes")
            return {"index_id": active, "path": value, "start_line": start_line, "end_line": end_line, "content": content.decode("utf-8", errors="replace")}
        else:
            raise IndexError("unknown_query", "unknown query command")
        items = [dict(row) for row in rows[:limit]]
        if command in {"search", "show"}:
            for item in items:
                item["anchors"] = json.loads(item["anchors"])
                item["units"] = [r[0] for r in connection.execute("SELECT source_file FROM symbol_units WHERE index_id=? AND usr=? ORDER BY source_file", (active, item["usr"]))]
        if command in {"callers", "callees"}:
            for item in items:
                item["anchors"] = json.loads(item["anchors"])
                item["units"] = [r[0] for r in connection.execute("SELECT source_file FROM call_units WHERE index_id=? AND instance_id=? ORDER BY source_file", (active, item["instance_id"]))]
        return {"index_id": active, "items": items, "truncated": len(rows) > limit}
    except sqlite3.Error as error:
        raise IndexError("database_error", "database query failed") from error
    finally:
        connection.close()
