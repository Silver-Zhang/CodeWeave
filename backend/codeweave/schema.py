"""M2 metadata shares the fact transaction. Legacy databases are never upgraded on read."""
import os
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile

from codeweave.index import SCHEMA, IndexError, _connection, _schema_identity

SCHEMA4 = SCHEMA.replace('PRAGMA user_version = 3;', '') + """
CREATE TABLE snapshots (
 id TEXT PRIMARY KEY, manifest TEXT NOT NULL, source_kind TEXT NOT NULL,
 base_git_head TEXT, represented_git_commit TEXT);
CREATE TABLE workspaces (
 id TEXT PRIMARY KEY, config_identity TEXT NOT NULL, generation INTEGER NOT NULL CHECK(generation>=0),
 latest_successful_index TEXT REFERENCES indexes(id));
CREATE TABLE refresh_attempts (
 id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces(id),
 base_generation INTEGER NOT NULL, token TEXT NOT NULL, source_kind TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('queued','capturing','sealed','analyzing','validating','publishing','published','failed','cancelling','cancelled','interrupted')),
 result_index_id TEXT REFERENCES indexes(id), error_code TEXT, message TEXT NOT NULL DEFAULT '',
 owner_pid INTEGER, owner_start TEXT,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 UNIQUE(workspace_id,token));
CREATE TABLE index_metadata (
 index_id TEXT PRIMARY KEY REFERENCES indexes(id), snapshot_id TEXT REFERENCES snapshots(id),
 workspace_id TEXT REFERENCES workspaces(id), profile_id TEXT, policy TEXT NOT NULL, source_kind TEXT NOT NULL,
 provenance TEXT NOT NULL);
CREATE TABLE symbol_evidence (
 index_id TEXT NOT NULL, usr TEXT NOT NULL, evidence TEXT NOT NULL,
 PRIMARY KEY(index_id,usr), FOREIGN KEY(index_id,usr) REFERENCES symbols(index_id,usr));
CREATE TABLE call_evidence (
 index_id TEXT NOT NULL, instance_id TEXT NOT NULL, evidence TEXT NOT NULL,
 PRIMARY KEY(index_id,instance_id), FOREIGN KEY(index_id,instance_id) REFERENCES calls(index_id,instance_id));
CREATE TABLE attempt_units (
 attempt_id TEXT NOT NULL REFERENCES refresh_attempts(id), source_file TEXT NOT NULL,
 state TEXT NOT NULL, diagnostics TEXT NOT NULL, command TEXT NOT NULL,
 PRIMARY KEY(attempt_id,source_file));
CREATE INDEX attempts_by_workspace ON refresh_attempts(workspace_id,created_at,id);
PRAGMA user_version = 4;
"""


def initialize(path: Path) -> None:
    """No-overwrite creation; an existing schema3 requires explicit new-path migration."""
    if path.exists():
        c = _connection(path, True)
        try:
            validate(c)
            if c.execute('PRAGMA user_version').fetchone()[0] != 4:
                raise IndexError('schema_incompatible', 'workspace requires schema4; migrate to a new path')
        finally:
            c.close()
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.codeweave-init-', dir=path.parent)
    os.close(fd)
    try:
        with closing(sqlite3.connect(name)) as c:
            c.executescript(SCHEMA4)
            c.commit()
        with open(name, 'rb') as f:
            os.fsync(f.fileno())
        os.link(name, path)
        sync_directory(path.parent)
    finally:
        Path(name).unlink(missing_ok=True)


def sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def validate(c: sqlite3.Connection) -> None:
    version = c.execute('PRAGMA user_version').fetchone()[0]
    if version not in (3, 4):
        raise IndexError('schema_incompatible', 'unsupported database schema')
    expected = sqlite3.connect(':memory:')
    try:
        expected.executescript(SCHEMA4 if version == 4 else SCHEMA)
        if _schema_identity(c) != _schema_identity(expected):
            raise IndexError('schema_incompatible', 'database schema does not match its version')
    finally:
        expected.close()


def migrate(source: Path, destination: Path) -> dict:
    if destination.exists() or source.resolve() == destination.resolve():
        raise IndexError('destination_exists', 'migration requires a new destination path')
    old = _connection(source, True)
    temporary = None
    try:
        validate(old)
        if old.execute('PRAGMA user_version').fetchone()[0] != 3:
            raise IndexError('schema_incompatible', 'migration accepts schema3 only')
        old.execute('BEGIN')
        if old.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or old.execute('PRAGMA foreign_key_check').fetchall():
            raise IndexError('database_error', 'legacy database integrity check failed')
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix='.codeweave-migrate-', dir=destination.parent)
        os.close(fd)
        temporary = Path(name)
        with closing(sqlite3.connect(name)) as new:
            new.execute('PRAGMA foreign_keys=ON')
            new.executescript(SCHEMA4)
            for table in ('indexes', 'files', 'units', 'unit_files', 'symbols', 'symbol_units', 'calls', 'call_units'):
                cursor = old.execute('SELECT * FROM ' + table)
                placeholders = ','.join('?' for _ in cursor.description)
                new.executemany(f'INSERT INTO {table} VALUES ({placeholders})', cursor)
            new.execute("INSERT INTO index_metadata(index_id,policy,source_kind,provenance) SELECT id,'legacy','legacy_observed_inputs','{}' FROM indexes")
            if new.execute('PRAGMA foreign_key_check').fetchall():
                raise IndexError('database_error', 'migration relationship check failed')
            new.commit()
        with temporary.open('rb') as f:
            os.fsync(f.fileno())
        os.link(temporary, destination)
        sync_directory(destination.parent)
        return {'schema_version': 4, 'database': str(destination), 'original_unchanged': True}
    finally:
        old.close()
        if temporary:
            temporary.unlink(missing_ok=True)
