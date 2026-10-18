"""Persistent CodeWeave M0 index backed by SQLite and Clang facts."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import uuid


class IndexError(Exception):
    pass


def _emit(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _connection(database: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    return connection


def _init(connection: sqlite3.Connection) -> None:
    connection.executescript("""
    PRAGMA foreign_keys = ON;
    CREATE TABLE IF NOT EXISTS indexes (
      id TEXT PRIMARY KEY, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      compilation_database TEXT NOT NULL, compilation_sha256 TEXT NOT NULL, coverage TEXT NOT NULL, diagnostic TEXT
    );
    CREATE TABLE IF NOT EXISTS files (
      index_id TEXT NOT NULL REFERENCES indexes(id), path TEXT NOT NULL, content TEXT NOT NULL,
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
      column_number INTEGER NOT NULL
    );
    """)


def _position(value: dict[str, object] | None) -> tuple[str | None, int | None, int | None]:
    if value is None:
        return None, None, None
    return str(value["file"]), int(value["line"]), int(value["column"])


def _load_compilation_files(directory: Path) -> list[Path]:
    path = directory / "compile_commands.json"
    if not path.is_file():
        raise IndexError(f"missing compilation database: {path}")
    try:
        commands = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise IndexError(f"invalid compilation database: {path}: {error}") from error
    if not isinstance(commands, list) or not commands:
        raise IndexError(f"compilation database has no commands: {path}")
    files: list[Path] = []
    for command in commands:
        if not isinstance(command, dict) or not isinstance(command.get("file"), str):
            raise IndexError("compilation database contains an invalid command")
        candidate = Path(command["file"])
        if not candidate.is_absolute():
            candidate = Path(str(command.get("directory", directory))) / candidate
        candidate = candidate.resolve()
        if not candidate.is_file():
            raise IndexError(f"compilation input is unavailable: {candidate}")
        files.append(candidate)
    return sorted(set(files))


def build_index(database: Path, compilation_database: Path, analyzer: Path) -> dict[str, object]:
    compilation_database = compilation_database.resolve()
    files = _load_compilation_files(compilation_database)
    if not analyzer.is_file() or not os.access(analyzer, os.X_OK):
        raise IndexError(f"analyzer is not executable: {analyzer}")
    with tempfile.TemporaryDirectory(prefix="codeweave-facts-") as temporary:
        facts = Path(temporary) / "facts.jsonl"
        result = subprocess.run(
            [str(analyzer), "--compilation-database", str(compilation_database), "--output", str(facts)],
            text=True, capture_output=True, check=False,
        )
        if result.returncode:
            message = result.stderr.strip() or "analyzer failed without a diagnostic"
            raise IndexError(f"analysis failed: {message}")
        try:
            records = [json.loads(line) for line in facts.read_text(encoding="utf-8").splitlines() if line]
        except (OSError, json.JSONDecodeError) as error:
            raise IndexError(f"analyzer produced invalid facts: {error}") from error
    entity_count = sum(record.get("type") == "entity" for record in records)
    if not entity_count:
        raise IndexError("analysis produced no entities; previous index remains unchanged")
    snapshot_files = set(files)
    for record in records:
        if record["type"] == "entity":
            snapshot_files.add(Path(str(record["declaration"]["file"])))
            if record["definition"]:
                snapshot_files.add(Path(str(record["definition"]["file"])))
        elif record["type"] == "call":
            snapshot_files.add(Path(str(record["location"]["file"])))
    unavailable = [path for path in snapshot_files if not path.is_file()]
    if unavailable:
        raise IndexError(f"analysis referenced unavailable source: {unavailable[0]}")
    index_id = str(uuid.uuid4())
    connection = _connection(database)
    try:
        _init(connection)
        with connection:
            connection.execute("INSERT INTO indexes(id, compilation_database, compilation_sha256, coverage) VALUES (?, ?, ?, ?)",
                               (index_id, str(compilation_database), hashlib.sha256((compilation_database / "compile_commands.json").read_bytes()).hexdigest(), "complete"))
            for source in sorted(snapshot_files):
                content = source.read_text(encoding="utf-8")
                connection.execute("INSERT INTO files VALUES (?, ?, ?, ?)",
                                   (index_id, str(source), content, hashlib.sha256(content.encode()).hexdigest()))
            for record in records:
                if record["type"] == "entity":
                    declaration = record["declaration"]
                    definition = record["definition"]
                    connection.execute("""INSERT OR REPLACE INTO symbols VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (index_id, record["usr"], record["name"], record["kind"], declaration["file"],
                          declaration["line"], declaration["column"], *_position(definition)))
                elif record["type"] == "call":
                    location = record["location"]
                    connection.execute("INSERT INTO calls(index_id, caller_usr, target_usr, dispatch, file, line, column_number) VALUES (?, ?, ?, ?, ?, ?, ?)",
                                       (index_id, record["caller_usr"], record["target_usr"], record["dispatch"], location["file"], location["line"], location["column"]))
    finally:
        connection.close()
    return {"index_id": index_id, "coverage": "complete", "entities": entity_count,
            "calls": sum(record.get("type") == "call" for record in records)}


def _index(connection: sqlite3.Connection, index_id: str | None) -> str:
    if index_id:
        row = connection.execute("SELECT id FROM indexes WHERE id = ?", (index_id,)).fetchone()
    else:
        row = connection.execute("SELECT id FROM indexes ORDER BY created_at DESC, rowid DESC LIMIT 1").fetchone()
    if not row:
        raise IndexError("no matching index")
    return str(row["id"])


def _symbol_rows(connection: sqlite3.Connection, active: str, rows: list[sqlite3.Row], limit: int) -> dict[str, object]:
    truncated = len(rows) > limit
    return {"index_id": active, "items": [dict(row) for row in rows[:limit]], "truncated": truncated}


def query(database: Path, command: str, value: str | None = None, index_id: str | None = None,
          limit: int = 50, start_line: int | None = None, end_line: int | None = None) -> dict[str, object]:
    if limit < 1 or limit > 200:
        raise IndexError("limit must be from 1 to 200")
    if not database.is_file():
        raise IndexError(f"index database does not exist: {database}")
    connection = _connection(database)
    try:
        active = _index(connection, index_id)
        if command == "status":
            row = connection.execute("SELECT * FROM indexes WHERE id = ?", (active,)).fetchone()
            return {"index_id": active, "coverage": row["coverage"], "compilation_database": row["compilation_database"], "compilation_sha256": row["compilation_sha256"]}
        if command == "search":
            rows = connection.execute("SELECT * FROM symbols WHERE index_id = ? AND name LIKE ? ORDER BY name LIMIT ?", (active, f"%{value or ''}%", limit + 1)).fetchall()
        elif command == "show":
            rows = connection.execute("SELECT * FROM symbols WHERE index_id = ? AND usr = ?", (active, value)).fetchall()
        elif command in {"callers", "callees"}:
            other_field = "caller_usr" if command == "callers" else "target_usr"
            filter_field = "target_usr" if command == "callers" else "caller_usr"
            rows = connection.execute(f"SELECT calls.*, symbols.name AS other_name FROM calls LEFT JOIN symbols ON symbols.index_id = calls.index_id AND symbols.usr = calls.{other_field} WHERE calls.index_id = ? AND calls.{filter_field} = ? ORDER BY calls.file, calls.line LIMIT ?", (active, value, limit + 1)).fetchall()
        elif command == "source":
            if start_line is None or end_line is None or start_line < 1 or end_line < start_line:
                raise IndexError("source requires a valid inclusive line range")
            row = connection.execute("SELECT content FROM files WHERE index_id = ? AND path = ?", (active, value)).fetchone()
            if not row:
                raise IndexError("path is not registered in this index")
            lines = row["content"].splitlines()
            if end_line > len(lines):
                raise IndexError("source range exceeds the indexed snapshot")
            return {"index_id": active, "path": value, "start_line": start_line, "end_line": end_line,
                    "content": "\n".join(lines[start_line - 1:end_line])}
        else:
            raise IndexError(f"unknown query command: {command}")
        truncated = len(rows) > limit
        return {"index_id": active, "items": [dict(row) for row in rows[:limit]], "truncated": truncated}
    finally:
        connection.close()
