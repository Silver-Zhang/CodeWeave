"""Versioned, sandboxed CodeWeave M0 SQLite index."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import sqlite3
import subprocess
import tempfile
import uuid

SCHEMA_VERSION = 2
FACT_SCHEMA_VERSION = 1
MAX_SOURCE_LINES = 200
MAX_SOURCE_BYTES = 32_768


class IndexError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _resolve(path: Path) -> Path:
    try:
        return path.expanduser().resolve(strict=True)
    except OSError as error:
        raise IndexError("invalid_path", str(error)) from error


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _connection(database: Path) -> sqlite3.Connection:
    try:
        connection = sqlite3.connect(database)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection
    except sqlite3.Error as error:
        raise IndexError("database_error", "cannot open index database") from error


def _init(connection: sqlite3.Connection) -> None:
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version not in (0, SCHEMA_VERSION):
        raise IndexError("schema_incompatible", "database schema is incompatible; rebuild into a new database")
    connection.executescript("""
    CREATE TABLE IF NOT EXISTS indexes (
      id TEXT PRIMARY KEY, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      compilation_database TEXT NOT NULL, compilation_sha256 TEXT NOT NULL,
      project_root TEXT NOT NULL, coverage TEXT NOT NULL, diagnostic TEXT,
      schema_version INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS files (
      index_id TEXT NOT NULL REFERENCES indexes(id), path TEXT NOT NULL, content BLOB NOT NULL,
      sha256 TEXT NOT NULL, PRIMARY KEY (index_id, path)
    );
    CREATE TABLE IF NOT EXISTS symbols (
      index_id TEXT NOT NULL REFERENCES indexes(id), usr TEXT NOT NULL, name TEXT NOT NULL,
      kind TEXT NOT NULL, declaration_file TEXT NOT NULL, declaration_line INTEGER NOT NULL,
      declaration_column INTEGER NOT NULL, definition_file TEXT, definition_line INTEGER,
      definition_column INTEGER, PRIMARY KEY (index_id, usr)
    );
    CREATE TABLE IF NOT EXISTS calls (
      id INTEGER PRIMARY KEY, index_id TEXT NOT NULL REFERENCES indexes(id), caller_usr TEXT,
      target_usr TEXT, dispatch TEXT NOT NULL, file TEXT NOT NULL, line INTEGER NOT NULL,
      column_number INTEGER NOT NULL, UNIQUE(index_id, caller_usr, target_usr, dispatch, file, line, column_number)
    );
    CREATE TABLE IF NOT EXISTS units (
      index_id TEXT NOT NULL REFERENCES indexes(id), source_file TEXT NOT NULL,
      command_sha256 TEXT NOT NULL, status TEXT NOT NULL, diagnostics TEXT NOT NULL,
      PRIMARY KEY(index_id, source_file, command_sha256)
    );
    CREATE INDEX IF NOT EXISTS calls_by_caller ON calls(index_id, caller_usr);
    CREATE INDEX IF NOT EXISTS calls_by_target ON calls(index_id, target_usr);
    PRAGMA user_version = 2;
    """)


def _arguments(entry: dict[str, object]) -> list[str]:
    if isinstance(entry.get("arguments"), list) and all(isinstance(arg, str) for arg in entry["arguments"]):
        return list(entry["arguments"])
    if isinstance(entry.get("command"), str):
        try:
            return shlex.split(entry["command"], posix=True)
        except ValueError as error:
            raise IndexError("invalid_compilation_database", "invalid command quoting") from error
    raise IndexError("invalid_compilation_database", "compile command has no arguments")


def _safe_arguments(arguments: list[str], source: Path, directory: Path) -> list[str]:
    if not arguments:
        raise IndexError("invalid_compilation_database", "empty compiler command")
    compiler = Path(arguments[0]).name
    if compiler not in {"c++", "g++", "clang++", "clang"}:
        raise IndexError("unsafe_compilation_argument", "compiler executable is not allowed")
    safe = [arguments[0]]
    value_flags = {"-I", "-isystem", "-iquote", "-include", "-imacros", "-D", "-U", "-std", "-x", "-target", "--target"}
    rejected = {"-MF", "-MT", "-MQ", "-MJ", "-serialize-diagnostics", "-fmodule-cache-path", "-Xclang", "-Xassembler", "-Xlinker", "-load", "-plugin"}
    index = 1
    while index < len(arguments):
        arg = arguments[index]
        if arg.startswith("@") or arg in rejected or arg.startswith("-fmodules") or arg.startswith("-plugin"):
            raise IndexError("unsafe_compilation_argument", f"rejected compiler argument: {arg}")
        if arg == "-o":
            if index + 1 >= len(arguments):
                raise IndexError("invalid_compilation_database", "output flag lacks value")
            index += 2
            continue
            index += 1
            continue
        if arg in {"-c", str(source), source.name}:
            index += 1
            continue
        if arg in value_flags:
            if index + 1 >= len(arguments):
                raise IndexError("invalid_compilation_database", f"compiler flag lacks value: {arg}")
            value = arguments[index + 1]
            if arg in {"-I", "-isystem", "-iquote", "-include", "-imacros"} and not Path(value).is_absolute():
                value = str((directory / value).resolve())
            safe.extend([arg, value])
            index += 2
            continue
        if arg.startswith(("-I", "-D", "-U", "-std=", "--target=", "-O", "-W", "-fno-", "-m")) or arg in {"-g", "-pthread", "-fPIC", "-pipe"}:
            safe.append(arg)
            index += 1
            continue
        if arg.startswith("-"):
            raise IndexError("unsafe_compilation_argument", f"unsupported compiler argument: {arg}")
        if Path(arg).resolve() != source:
            raise IndexError("unsafe_compilation_argument", f"unexpected positional argument: {arg}")
        index += 1
    safe.extend(["-c", str(source)])
    return safe


def _load_compilation_database(directory: Path) -> tuple[list[dict[str, object]], Path, bytes]:
    directory = _resolve(directory)
    path = directory / "compile_commands.json"
    if not path.is_file():
        raise IndexError("missing_compilation_database", f"missing compilation database: {path}")
    try:
        raw = path.read_bytes()
        commands = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise IndexError("invalid_compilation_database", "compile_commands.json is invalid UTF-8 JSON") from error
    if not isinstance(commands, list) or not commands:
        raise IndexError("invalid_compilation_database", "compilation database has no commands")
    normalized: list[dict[str, object]] = []
    sources: list[Path] = []
    seen: set[tuple[str, str]] = set()
    for entry in commands:
        if not isinstance(entry, dict) or not isinstance(entry.get("file"), str):
            raise IndexError("invalid_compilation_database", "compile command lacks file")
        command_directory = _resolve(Path(str(entry.get("directory", directory))))
        source = Path(entry["file"])
        source = _resolve(source if source.is_absolute() else command_directory / source)
        if not source.is_file():
            raise IndexError("missing_input", f"compilation input is unavailable: {source}")
        arguments = _safe_arguments(_arguments(entry), source, command_directory)
        digest = _sha256(json.dumps(arguments, separators=(",", ":")).encode())
        key = (str(source), digest)
        if key in seen:
            continue
        seen.add(key)
        sources.append(source)
        normalized.append({"directory": str(command_directory), "file": str(source), "arguments": arguments, "command_sha256": digest})
    root = Path(os.path.commonpath([str(source.parent) for source in sources])).resolve()
    if not root.is_dir():
        raise IndexError("invalid_compilation_database", "cannot determine project root")
    return normalized, root, raw


def _sandbox_command(analyzer: Path, database_directory: Path, facts: Path, project_root: Path, allowed_directories: set[Path]) -> list[str]:
    bwrap = shutil.which("bwrap")
    if not bwrap:
        raise IndexError("sandbox_unavailable", "bubblewrap is required for untrusted compilation databases")
    mounts = ["--die-with-parent", "--unshare-net", "--new-session", "--proc", "/proc", "--dev", "/dev"]
    for root in (Path("/usr"), Path("/lib"), Path("/lib64"), analyzer.parent, database_directory, project_root, *allowed_directories):
        if root.exists():
            mounts.extend(["--ro-bind", str(root), str(root)])
    facts.parent.mkdir(parents=True, exist_ok=True)
    mounts.extend(["--bind", str(facts.parent), str(facts.parent)])
    return [bwrap, *mounts, str(analyzer), "--compilation-database", str(database_directory), "--output", str(facts), "--project-root", str(project_root)]


def _validate_fact(record: object, project_root: Path) -> dict[str, object]:
    if not isinstance(record, dict) or record.get("schema") != FACT_SCHEMA_VERSION or record.get("type") not in {"entity", "call", "file"}:
        raise IndexError("invalid_facts", "analyzer returned an unsupported fact record")
    if record["type"] == "file":
        path = record.get("path")
        if not isinstance(path, str) or not isinstance(record.get("content"), str):
            raise IndexError("invalid_facts", "file fact lacks a physical buffer")
        resolved = _resolve(Path(path))
        if not _inside(resolved, project_root):
            raise IndexError("outside_project_root", "analyzer fact references a file outside the project root")
        record["path"] = str(resolved)
        return record
    for key in ("declaration", "definition") if record["type"] == "entity" else ("location",):
        location = record.get(key)
        if location is None and key == "definition":
            continue
        if not isinstance(location, dict) or not isinstance(location.get("file"), str):
            raise IndexError("invalid_facts", "fact lacks a physical source location")
        path = _resolve(Path(location["file"]))
        if not _inside(path, project_root):
            raise IndexError("outside_project_root", "analyzer fact references a file outside the project root")
        location["file"] = str(path)
    return record


def build_index(database: Path, compilation_database: Path, analyzer: Path) -> dict[str, object]:
    analyzer = _resolve(analyzer)
    if not analyzer.is_file() or not os.access(analyzer, os.X_OK):
        raise IndexError("analyzer_unavailable", "analyzer is not executable")
    commands, project_root, raw_database = _load_compilation_database(compilation_database)
    allowed_directories = {Path(command["directory"]) for command in commands}
    with tempfile.TemporaryDirectory(prefix="codeweave-index-") as temporary:
        staging = Path(temporary)
        sanitized_directory = staging / "compilation"
        sanitized_directory.mkdir()
        sanitized_commands = [{"directory": command["directory"], "file": command["file"], "arguments": command["arguments"]} for command in commands]
        (sanitized_directory / "compile_commands.json").write_text(json.dumps(sanitized_commands), encoding="utf-8")
        facts = staging / "output" / "facts.jsonl"
        result = subprocess.run(_sandbox_command(analyzer, sanitized_directory, facts, project_root, allowed_directories), text=True, capture_output=True, check=False)
        if result.returncode:
            message = result.stderr.strip() or "sandboxed analyzer failed"
            raise IndexError("analysis_failed", message)
        try:
            analyzer_stderr = result.stderr.strip()
            records = [_validate_fact(json.loads(line), project_root) for line in facts.read_text(encoding="utf-8").splitlines() if line]
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, IndexError) as error:
            if isinstance(error, IndexError):
                raise
            raise IndexError("invalid_facts", "analyzer produced invalid facts") from error
    entities = [record for record in records if record["type"] == "entity"]
    file_facts = {record["path"]: record["content"].encode("utf-8") for record in records if record["type"] == "file"}
    if not entities:
        raise IndexError("empty_analysis", "analysis produced no entities; previous index remains unchanged")
    snapshot_paths = {Path(command["file"]) for command in commands}
    for record in records:
        for location in (record.get("declaration"), record.get("definition"), record.get("location")):
            if location:
                snapshot_paths.add(Path(location["file"]))
    snapshots: dict[str, bytes] = {}
    for path in snapshot_paths:
        resolved = _resolve(path)
        if not _inside(resolved, project_root):
            raise IndexError("outside_project_root", "snapshot file escapes project root")
        content = file_facts.get(str(resolved))
        if content is None:
            raise IndexError("incomplete_snapshot", f"analyzer did not provide parsed buffer: {resolved}")
        snapshots[str(resolved)] = content
    index_id = str(uuid.uuid4())
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = _connection(database)
    try:
        _init(connection)
        with connection:
            connection.execute("INSERT INTO indexes VALUES (?, CURRENT_TIMESTAMP, ?, ?, ?, ?, ?, ?)",
                               (index_id, str(_resolve(compilation_database)), _sha256(raw_database), str(project_root), "units_complete", analyzer_stderr, SCHEMA_VERSION))
            for path, content in snapshots.items():
                connection.execute("INSERT INTO files VALUES (?, ?, ?, ?)", (index_id, path, content, _sha256(content)))
            merged: dict[str, dict[str, object]] = {}
            for record in entities:
                prior = merged.get(record["usr"])
                if prior and prior.get("definition") and record.get("definition") and prior["definition"] != record["definition"]:
                    raise IndexError("conflicting_definition", f"conflicting definitions for {record['name']}")
                if not prior or (not prior.get("definition") and record.get("definition")):
                    merged[record["usr"]] = record
            for record in merged.values():
                declaration, definition = record["declaration"], record.get("definition")
                connection.execute("INSERT INTO symbols VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (index_id, record["usr"], record["name"], record["kind"], declaration["file"], declaration["line"], declaration["column"], *(None if not definition else definition["file"], None if not definition else definition["line"], None if not definition else definition["column"])))
            for record in records:
                if record["type"] == "call":
                    location = record["location"]
                    connection.execute("INSERT OR IGNORE INTO calls(index_id, caller_usr, target_usr, dispatch, file, line, column_number) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (index_id, record.get("caller_usr"), record.get("target_usr"), record["dispatch"], location["file"], location["line"], location["column"]))
            for command in commands:
                connection.execute("INSERT INTO units VALUES (?, ?, ?, ?, ?)", (index_id, command["file"], command["command_sha256"], "complete", "[]"))
    except sqlite3.Error as error:
        raise IndexError("database_error", "database transaction failed") from error
    finally:
        connection.close()
    return {"index_id": index_id, "coverage": "units_complete", "entities": len(merged), "calls": sum(record["type"] == "call" for record in records)}


def _index(connection: sqlite3.Connection, index_id: str | None) -> str:
    row = connection.execute("SELECT id FROM indexes WHERE id = ?", (index_id,)).fetchone() if index_id else connection.execute("SELECT id FROM indexes ORDER BY rowid DESC LIMIT 1").fetchone()
    if not row:
        raise IndexError("index_not_found", "no matching index")
    return str(row["id"])


def query(database: Path, command: str, value: str | None = None, index_id: str | None = None, limit: int = 50, start_line: int | None = None, end_line: int | None = None) -> dict[str, object]:
    if not 1 <= limit <= 200:
        raise IndexError("invalid_limit", "limit must be from 1 to 200")
    if command in {"show", "callers", "callees", "source"} and not value:
        raise IndexError("missing_argument", f"{command} requires a value")
    if not database.is_file():
        raise IndexError("database_not_found", "index database does not exist")
    connection = _connection(database)
    try:
        _init(connection)
        active = _index(connection, index_id)
        if command == "status":
            row = connection.execute("SELECT * FROM indexes WHERE id = ?", (active,)).fetchone()
            return {"index_id": active, "coverage": row["coverage"], "compilation_database": row["compilation_database"], "compilation_sha256": row["compilation_sha256"], "schema_version": row["schema_version"]}
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
            if not row:
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
        return {"index_id": active, "items": [dict(row) for row in rows[:limit]], "truncated": len(rows) > limit}
    except sqlite3.Error as error:
        raise IndexError("database_error", "database query failed") from error
    finally:
        connection.close()
