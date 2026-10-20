"""Trusted local configuration, full saved-workspace refresh, and atomic publication."""
from __future__ import annotations
from contextlib import contextmanager
import base64
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import resource
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid

from codeweave import git_snapshot, snapshot
from codeweave.index import (IndexError, _arguments, _collect, _connection, _json, _safe_arguments,
                             _sha256, _status_size, _status_value, _trusted_root, insert_facts)
from codeweave.schema import initialize, validate, sync_directory

TERMINAL = {'published', 'failed', 'cancelled', 'interrupted'}
POLICY = 'm2a-v1'


@contextmanager
def writer(database):
    connection = _connection(database)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def process_start(pid: int) -> str | None:
    try:
        return Path(f'/proc/{pid}/stat').read_text().rsplit(') ',1)[1].split()[19]
    except (OSError,IndexError):
        return None


def configure(state: Path, project: Path, compilation: Path, analyzer: Path, resource_dir: Path,
              dependencies: list[Path] | None = None, toolchains: list[Path] | None = None,
              head_compilation: Path | None = None) -> dict:
    project = _trusted_root(project)
    state = state.absolute().resolve()
    dependencies = list(dict.fromkeys(_trusted_root(p) for p in dependencies or []))
    toolchains = list(dict.fromkeys(_trusted_root(p) for p in toolchains or []))
    if any(state.is_relative_to(root) for root in [project, *dependencies, *toolchains]):
        raise IndexError('invalid_state_directory', 'state/cache/database must be outside analyzed input roots')
    compilation = compilation.resolve(strict=True)
    if compilation.is_dir():
        compilation /= 'compile_commands.json'
    analyzer = analyzer.resolve(strict=True)
    resource_dir = _trusted_root(resource_dir)
    if not (resource_dir/'include/stddef.h').is_file() or not os.access(analyzer, os.X_OK):
        raise IndexError('toolchain_unavailable', 'provide a built matching analyzer and Clang resource directory')
    if not shutil.which('bwrap'):
        raise IndexError('sandbox_unavailable', 'Bubblewrap is required')
    snapshot.read_regular(compilation, 16_777_216)
    if head_compilation:
        head_compilation = head_compilation.resolve(strict=True)
        if head_compilation.is_dir():
            head_compilation /= 'compile_commands.json'
    values = {'version': 1, 'project_root': str(project), 'compilation_database': str(compilation),
              'analyzer': str(analyzer), 'resource_dir': str(resource_dir),
              'dependency_roots': list(map(str, dependencies)), 'toolchain_roots': list(map(str, toolchains)),
              'head_compilation_database': str(head_compilation) if head_compilation else None}
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    state.chmod(0o700)
    path = state/'workspace.json'
    if path.exists():
        existing = json.loads(snapshot.read_regular(path, 32_768))
        if any(existing.get(k) != v for k, v in values.items()):
            raise IndexError('configuration_conflict', 'state is configured for different inputs; use a new state directory')
        return existing
    values['workspace_id'] = str(uuid.uuid4())
    initialize(state/'index.sqlite3')
    with writer(state/'index.sqlite3') as c:
        c.execute('INSERT INTO workspaces VALUES (?,?,0,NULL)', (values['workspace_id'], _sha256(_json(values).encode())))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as f:
        f.write(_json(values))
        f.flush()
        os.fsync(f.fileno())
    sync_directory(state)
    return values


class Workspace:
    def __init__(self, state: Path):
        self.state = state.absolute().resolve(strict=True)
        self.config = json.loads(snapshot.read_regular(self.state/'workspace.json', 32_768))
        required = {'version','workspace_id','project_root','compilation_database','analyzer','resource_dir','dependency_roots','toolchain_roots','head_compilation_database'}
        if not isinstance(self.config,dict) or set(self.config)!=required or self.config['version']!=1:
            raise IndexError('configuration_conflict','invalid workspace configuration; use a valid state directory')
        if any(not isinstance(self.config[k],str) for k in ('workspace_id','project_root','compilation_database','analyzer','resource_dir')) or any(not isinstance(self.config[k],list) or any(not isinstance(p,str) for p in self.config[k]) for k in ('dependency_roots','toolchain_roots')):
            raise IndexError('configuration_conflict','invalid workspace paths')
        self.database = self.state/'index.sqlite3'
        self.id = self.config['workspace_id']
        c = _connection(self.database, True)
        try:
            validate(c)
            row = c.execute('SELECT config_identity FROM workspaces WHERE id=?', (self.id,)).fetchone()
            if not row or row[0] != _sha256(_json(self.config).encode()):
                raise IndexError('configuration_conflict', 'workspace configuration identity does not match database')
        finally:
            c.close()

    def recover(self) -> None:
        with (self.state/'refresh.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            with writer(self.database) as c:
                for row in c.execute("SELECT id,owner_pid,owner_start FROM refresh_attempts WHERE workspace_id=? AND state NOT IN ('published','failed','cancelled','interrupted')", (self.id,)).fetchall():
                    if not row['owner_pid'] or process_start(row['owner_pid']) != row['owner_start']:
                        c.execute("UPDATE refresh_attempts SET state='interrupted',error_code='interrupted',message='Worker stopped before publication' WHERE id=?", (row['id'],))

    def status(self) -> dict:
        c = _connection(self.database, True)
        try:
            row = dict(c.execute('SELECT id,generation,latest_successful_index FROM workspaces WHERE id=?', (self.id,)).fetchone())
            attempt = c.execute('SELECT id FROM refresh_attempts WHERE workspace_id=? ORDER BY rowid DESC LIMIT 1', (self.id,)).fetchone()
            row.update(configured=True, project_root=_status_value(self.config['project_root']),
                       compilation_database=_status_value(self.config['compilation_database']),
                       refresh_mode='full', last_attempt=self.attempt(attempt[0]) if attempt else None,
                       disk_state='Refresh reads saved disk bytes; unsaved editor buffers are not indexed')
            return bounded(row, 32_768)
        finally:
            c.close()

    def attempt(self, request_id: str, limit: int = 20, offset: int = 0, details: bool = False,
                field: str | None = None, field_offset: int = 0) -> dict:
        validate_id(request_id)
        if not 1 <= limit <= 200 or not 0 <= offset < 2**63 or field_offset<0 or field_offset and not field:
            raise IndexError('invalid_arguments', 'invalid attempt pagination')
        c = _connection(self.database, True)
        try:
            row = c.execute('SELECT * FROM refresh_attempts WHERE id=? AND workspace_id=?', (request_id, self.id)).fetchone()
            if not row:
                raise IndexError('attempt_not_found', 'refresh request does not exist')
            result = {k: row[k] for k in ('id', 'source_kind', 'state', 'result_index_id', 'error_code', 'created_at', 'updated_at')}
            result.update(request_id=row['id'], message=_status_value(row['message']))
            result['unit_count'] = c.execute('SELECT count(*) FROM attempt_units WHERE attempt_id=?', (request_id,)).fetchone()[0]
            if field:
                if field not in ({'source_file','diagnostics','command'} if details else {'message'}) or details and limit!=1 or not details and offset:
                    raise IndexError('invalid_arguments','select one unit with limit 1, or the attempt message')
                value=c.execute('SELECT '+field+' FROM attempt_units WHERE attempt_id=? ORDER BY source_file LIMIT 1 OFFSET ?',(request_id,offset)).fetchone() if details else (row['message'],)
                if value is None:
                    raise IndexError('status_item_not_found','unit offset does not exist')
                raw=value[0].encode('utf-8')
                if field_offset>len(raw):
                    raise IndexError('invalid_offset','field offset exceeds stored bytes')
                chunk=raw[field_offset:field_offset+8192];end=field_offset+len(chunk)
                result.update(field=field,offset=offset,field_offset=field_offset,encoding='base64',content=base64.b64encode(chunk).decode('ascii'),
                              field_bytes=len(raw),field_sha256=_sha256(raw),returned_bytes=len(chunk),has_more=end<len(raw),next_field_offset=end if end<len(raw) else None)
            elif details:
                rows = c.execute('SELECT source_file,state,diagnostics,command FROM attempt_units WHERE attempt_id=? ORDER BY source_file LIMIT ? OFFSET ?', (request_id, limit, offset)).fetchall()
                items = [{k: _status_value(r[k]) for k in r.keys()} for r in rows]
                result.update(items=items, offset=offset, returned_count=len(items), total_count=result['unit_count'],
                              has_more=offset+len(items)<result['unit_count'], next_offset=offset+len(items) if offset+len(items)<result['unit_count'] else None)
                while _status_size(result) > 32_768 and result['items']:
                    result['items'].pop()
                    result.update(returned_count=len(result['items']), next_offset=offset+len(result['items']), has_more=True, budget_limited=True)
                if not result['items'] and offset<result['unit_count']:
                    raise IndexError('query_limit','one unit exceeds response budget; request field chunks')
            return bounded(result, 32_768)
        finally:
            c.close()

    def enqueue(self, generation: int, token: str, source_kind: str = 'workspace') -> dict:
        if not isinstance(source_kind,str) or source_kind not in {'workspace', 'head'} or not isinstance(token,str) or not 1 <= len(token) <= 128 or type(generation) is not int or not 0 <= generation < 2**63:
            raise IndexError('invalid_arguments', 'invalid refresh request')
        with writer(self.database) as c:
            c.execute('BEGIN IMMEDIATE')
            prior = c.execute('SELECT id,base_generation,source_kind FROM refresh_attempts WHERE workspace_id=? AND token=?', (self.id, token)).fetchone()
            if prior:
                if prior['base_generation'] != generation or prior['source_kind'] != source_kind:
                    raise IndexError('idempotency_conflict', 'token already identifies a different request')
                return {**self.attempt(prior['id']), 'created': False}
            current = c.execute('SELECT generation FROM workspaces WHERE id=?', (self.id,)).fetchone()[0]
            if current != generation:
                raise IndexError('workspace_generation_conflict', 'reload workspace generation before refreshing')
            active = c.execute("SELECT id FROM refresh_attempts WHERE workspace_id=? AND state NOT IN ('published','failed','cancelled','interrupted')", (self.id,)).fetchone()
            if active:
                raise IndexError('refresh_busy', 'active request: '+active[0])
            request_id = str(uuid.uuid4())
            c.execute('INSERT INTO refresh_attempts(id,workspace_id,base_generation,token,source_kind,state,owner_pid,owner_start) VALUES (?,?,?,?,?,?,?,?)', (request_id, self.id, generation, token, source_kind, 'queued',os.getpid(),process_start(os.getpid())))
        return {**self.attempt(request_id), 'created': True}

    def spawn(self, request_id: str) -> None:
        validate_id(request_id)
        log = self.state/(request_id+'.log')
        with log.open('ab') as output:
            try:
                process = subprocess.Popen([sys.executable, '-m', 'codeweave.workspace_worker', '--state', str(self.state), '--request-id', request_id],
                                           stdin=subprocess.DEVNULL, stdout=output, stderr=output, start_new_session=True)
                with writer(self.database) as c:
                    c.execute("UPDATE refresh_attempts SET owner_pid=?,owner_start=? WHERE id=? AND state='queued'", (process.pid,process_start(process.pid),request_id))
            except OSError as error:
                with writer(self.database) as c:
                    c.execute("UPDATE refresh_attempts SET state='failed',error_code='worker_unavailable',message=? WHERE id=?",(str(error),request_id))
                raise IndexError('worker_unavailable','cannot start owned refresh worker') from error

    def cancel(self, request_id: str) -> dict:
        self.attempt(request_id)
        with writer(self.database) as c:
            c.execute("UPDATE refresh_attempts SET state='cancelling',updated_at=CURRENT_TIMESTAMP WHERE id=? AND state NOT IN ('published','failed','cancelled','interrupted')", (request_id,))
        return self.attempt(request_id)

    def step(self, request_id: str, state: str) -> None:
        with writer(self.database) as c:
            row = c.execute('SELECT state FROM refresh_attempts WHERE id=?', (request_id,)).fetchone()
            if not row or row[0] == 'cancelling':
                raise IndexError('cancelled', 'refresh was cancelled')
            if row[0] in TERMINAL:
                raise IndexError('attempt_finished', 'request already finished')
            c.execute('UPDATE refresh_attempts SET state=?,updated_at=CURRENT_TIMESTAMP WHERE id=?', (state, request_id))

    def run(self, request_id: str) -> dict:
        attempt = self.attempt(request_id)
        if attempt['state'] in TERMINAL:
            return attempt
        with (self.state/'refresh.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise IndexError('refresh_busy', 'another owned worker holds the refresh lease') from error
            try:
                with writer(self.database) as c:
                    c.execute('UPDATE refresh_attempts SET owner_pid=?,owner_start=? WHERE id=?', (os.getpid(),process_start(os.getpid()),request_id))
                for retry in range(2):
                    try:
                        with tempfile.TemporaryDirectory(prefix='capture-', dir=self.state) as temporary:
                            self._analyze(request_id, Path(temporary), attempt['source_kind'])
                        break
                    except IndexError as error:
                        if error.code!='workspace_changed_during_capture' or retry:
                            raise
            except Exception as error:
                code = error.code if isinstance(error, IndexError) else 'refresh_failed'
                with writer(self.database) as c:
                    c.execute("UPDATE refresh_attempts SET state=?,error_code=?,message=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND state!='published'", ('cancelled' if code == 'cancelled' else 'failed', code, str(error), request_id))
        return self.attempt(request_id)

    def _analyze(self, request_id: str, stage: Path, source_kind: str) -> None:
        cfg = self.config
        project, analyzer, resource_dir = [Path(cfg[k]) for k in ('project_root', 'analyzer', 'resource_dir')]
        self.step(request_id, 'capturing')
        search, system_roots = snapshot.header_configuration(resource_dir, list(map(Path, cfg['toolchain_roots'])))
        dependency_roots = list(map(Path, cfg['dependency_roots']))
        roots = [(project, False), *((p, False) for p in dependency_roots), *((p, True) for p in system_roots)]
        try:
            before_git = git_snapshot.collect(project, stage/'git-before') if (project/'.git').exists() else {'base_git_head': None, 'tree': {}, 'staged': [], 'limitation': 'not a Git repository'}
        except IndexError as error:
            if source_kind=='head':
                raise
            before_git = {'base_git_head':None,'tree':{},'staged':[], 'limitation':error.code+': '+str(error)}
        raw = snapshot.read_regular(Path(cfg['compilation_database']), 16_777_216)
        manifests, mounts = snapshot.capture(roots, stage, {'.git'})
        if before_git.get('ref_identity') and git_snapshot.ref_identity(project)!=before_git['ref_identity']:
            raise IndexError('workspace_changed_during_capture','Git refs/index changed during input capture')
        if snapshot.read_regular(Path(cfg['compilation_database']), 16_777_216) != raw:
            raise IndexError('workspace_changed_during_capture', 'compilation database changed during capture')
        tree = before_git['tree']
        head = before_git['base_git_head']
        project_entries = manifests[0]['entries']
        project_files = {x['path']: x for x in project_entries if x['kind'] == 'file'}
        represented = head if head and set(project_files) == set(tree) and all(project_files[p]['sha256'] == tree[p]['sha256'] and bool(project_files[p]['mode'] & 0o111) == bool(tree[p]['mode'] & 0o111) for p in tree) else None
        if source_kind == 'head':
            if not head:
                raise IndexError('head_unavailable', 'no readable Git HEAD; workspace refresh remains available')
            baseline = cfg['head_compilation_database']
            if baseline:
                raw = snapshot.read_regular(Path(baseline), 16_777_216)
                profile_origin = 'explicit_baseline_configuration'
            else:
                try:
                    relative = Path(cfg['compilation_database']).relative_to(project).as_posix()
                    raw = tree[relative]['content']
                except (ValueError, KeyError) as error:
                    raise IndexError('head_configuration_unavailable', 'HEAD does not contain the configured compilation database; configure --head-compilation-database explicitly') from error
                profile_origin = 'git_tree_configuration'
            head_root = stage/'head-project'
            git_snapshot.export(tree, head_root)
            entries = snapshot.inventory(head_root)
            manifests[0] = {'root': str(project), 'entries': entries}
            mounts[0] = (head_root, project)
            represented = head
        else:
            profile_origin = 'saved_workspace_configuration'
        git_paths=before_git.get('index_entries',{})
        saved_entries={e['path']:e for e in manifests[0]['entries'] if e['kind']=='file'}
        untracked=sorted(set(saved_entries)-set(git_paths)) if head and source_kind=='workspace' else []
        unstaged=[]
        if git_paths and source_kind=='workspace':
            for path,item in git_paths.items():
                if path not in saved_entries:
                    unstaged.append(path);continue
                data=snapshot.read_regular(mounts[0][0]/path)
                oid=hashlib.new('sha256' if len(head)==64 else 'sha1',b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()
                if oid!=item['oid'] or bool(saved_entries[path]['mode']&0o111)!=bool(item['mode']&0o111):
                    unstaged.append(path)
        # Resolution occurs against sealed manifest aliases, never live paths.
        namespace = {str(Path(m['root'])/entry['path']): entry for m in manifests for entry in m['entries']}

        def resolve(path: Path) -> Path:
            parts = list(path.parts[1:]); current = Path('/'); links = 0
            while parts:
                part = parts.pop(0)
                if part in {'', '.'}:
                    continue
                if part == '..':
                    current = current.parent
                    continue
                current /= part
                item = namespace.get(str(current))
                if item and item['kind'] == 'link':
                    links += 1
                    if links > 40:
                        raise IndexError('unsupported_snapshot_link', 'cyclic sealed alias')
                    parts = list(Path(item['target']).parts[1:])+parts
                    current = Path('/')
            return current

        commands = compilation_commands(raw, project, [p for p, _ in roots], namespace, resolve)
        runtime = []
        runtime_identity = []
        for n, path in enumerate(snapshot.runtime_files(analyzer)):
            if any(path.is_relative_to(target) for _, target in mounts):
                raise IndexError('overlapping_snapshot_roots', 'analyzer runtime must be outside input roots')
            data = snapshot.read_regular(path.resolve(strict=True), 268_435_456)
            sealed = stage/f'runtime-{n}'
            sealed.write_bytes(data)
            sealed.chmod(0o500)
            runtime.append((sealed, path))
            runtime_identity.append({'path': str(path), 'sha256': _sha256(data)})
        toolchain = {'protocol': 3, 'runtime': runtime_identity, 'search_roots': list(map(str, search)),
                     'headers_sha256': _sha256(_json(manifests[1+len(dependency_roots):]).encode())}
        for command in commands:
            command['arguments'] += ['-nostdinc', '-nostdinc++', '-resource-dir', str(resource_dir)]
            for root in search:
                command['arguments'] += ['-isystem', str(root)]
            command['command_sha256'] = _sha256(_json({'command': command, 'toolchain': toolchain}).encode())
        profile = _sha256(_json({'commands': commands, 'toolchain': toolchain, 'policy': POLICY}).encode())
        manifest = {'version': 1, 'roots': manifests, 'runtime': runtime_identity, 'raw_ccdb_sha256': _sha256(raw),
                    'source_kind': source_kind, 'base_git_head': head, 'represented_git_commit': represented,
                    'capture_policy': 'sealed-vector-v1', 'profile_origin': profile_origin}
        snapshot_id = _sha256(_json(manifest).encode())
        self.step(request_id, 'sealed')
        ccdb = stage/'compilation'; ccdb.mkdir()
        (ccdb/'compile_commands.json').write_text(_json([{k: c[k] for k in ('directory', 'file', 'arguments')} for c in commands]))
        output = stage/'output'; output.mkdir()
        argv = snapshot.sandbox(analyzer, mounts, runtime, ccdb, output, project, {Path(c['directory']) for c in commands})
        self.step(request_id, 'analyzing')
        analysis_epoch=datetime.now(timezone.utc).isoformat()
        diagnostics = output/'diagnostics.txt'
        with diagnostics.open('wb') as log:
            process = subprocess.Popen(argv, stdout=log, stderr=log, env=snapshot.ENV, preexec_fn=worker_limits)
            try:
                deadline = time.monotonic()+180
                while process.poll() is None:
                    if self.attempt(request_id)['state'] == 'cancelling':
                        raise IndexError('cancelled', 'refresh was cancelled')
                    if time.monotonic() > deadline:
                        raise IndexError('analysis_timeout', 'analysis exceeded 180 seconds')
                    time.sleep(0.1)
            finally:
                if process.poll() is None:
                    process.kill(); process.wait()
        text = snapshot.read_regular(diagnostics, 268_435_456).decode('utf-8', 'replace')
        fact_path = output/'facts.jsonl'
        records = [json.loads(line) for line in snapshot.read_regular(fact_path, 268_435_456).splitlines() if line] if fact_path.exists() else []
        units = {r['tu']: r for r in records if r.get('type') == 'unit'}
        with writer(self.database) as c:
            c.executemany('INSERT INTO attempt_units VALUES (?,?,?,?,?)', [(request_id, cmd['file'], units.get(cmd['file'], {}).get('status', 'failed'), _json(units.get(cmd['file'], {}).get('diagnostics', [])), _json(cmd)) for cmd in commands])
        if process.returncode:
            raise IndexError('analysis_failed', text or 'isolated analyzer failed')
        self.step(request_id, 'validating')
        collected = _collect(records, commands, project, fact_schema=3, allow_empty=True)
        for source, target in mounts:
            entries = next(m['entries'] for m in manifests if m['root'] == str(target))
            snapshot.verify_copy(source, entries)
        for source, target in runtime:
            expected = next(x['sha256'] for x in runtime_identity if x['path'] == str(target))
            if _sha256(snapshot.read_regular(source, 268_435_456)) != expected:
                raise IndexError('sealed_input_changed', 'sealed runtime changed')
        for path, (_, digest, _) in collected[0].items():
            observed = namespace.get(path)
            if not observed or observed.get('kind') != 'file' or observed.get('sha256') != digest:
                raise IndexError('inconsistent_input', 'AST input was not the registered sealed buffer: '+path)
        validate_evidence(collected)
        selected = {c['file'] for c in commands}
        uncovered = sorted(path for path, item in namespace.items() if item['kind'] == 'file' and Path(path).is_relative_to(project) and Path(path).suffix in {'.cpp', '.cc', '.cxx', '.c'} and path not in selected)
        provenance = {'base_git_head': head, 'represented_git_commit': represented, 'profile_origin': profile_origin,
                      'uncovered_files': uncovered, 'staged_paths': before_git['staged'], 'git_limitation':before_git['limitation'],
                      'unstaged_paths':sorted(unstaged),'untracked_paths':untracked,
                      'analysis_epoch':analysis_epoch,'cache_eligible':False,
                      'volatile_builtin_policy':'full analysis at recorded epoch; no deterministic-output or TU-reuse claim',
                      'dependency_policy':'explicit dependency roots are frozen current inputs, including for HEAD; no historical dependency reconstruction',
                      'input_policy': 'sealed vector, not an atomic wall-clock filesystem instant'}
        try:
            provenance['workspace_changed_since_capture'] = snapshot.inventory(project, excluded={'.git'}) != project_entries if source_kind == 'workspace' else None
        except (OSError, IndexError):
            provenance['workspace_changed_since_capture'] = True
        self.step(request_id, 'publishing')
        index_id = str(uuid.uuid4())
        with writer(self.database) as c:
            c.execute('PRAGMA synchronous=FULL')
            c.execute('BEGIN IMMEDIATE')
            job = c.execute('SELECT * FROM refresh_attempts WHERE id=?', (request_id,)).fetchone()
            current = c.execute('SELECT generation,latest_successful_index FROM workspaces WHERE id=?', (self.id,)).fetchone()
            if job['state'] != 'publishing':
                raise IndexError('cancelled', 'publication cancelled')
            if current['generation'] != job['base_generation']:
                raise IndexError('workspace_generation_conflict', 'workspace advanced before publication')
            provenance['previous_index_id'] = current['latest_successful_index']
            c.execute('INSERT OR IGNORE INTO snapshots VALUES (?,?,?,?,?)', (snapshot_id, _json(manifest), source_kind, head, represented))
            c.execute('INSERT INTO indexes VALUES (?,CURRENT_TIMESTAMP,?,?,?,?,?,?,?)', (index_id, cfg['compilation_database'], _sha256(raw), str(project), 'units_complete', text, 4, _json(toolchain)))
            insert_facts(c, index_id, commands, collected)
            c.execute('INSERT INTO index_metadata VALUES (?,?,?,?,?,?,?)', (index_id, snapshot_id, self.id, profile, POLICY, source_kind, _json(provenance)))
            c.executemany('INSERT INTO symbol_evidence VALUES (?,?,?)', [(index_id, usr, _json({k: r.get(k) for k in ('semantic_usr','linkage','owner_tu','definition_range','normalized_definition')})) for usr, r in collected[2].items()])
            c.executemany('INSERT INTO call_evidence VALUES (?,?,?)', [(index_id, instance, _json(r.get('syntax'))) for instance, r in collected[3].items()])
            if source_kind == 'workspace':
                c.execute('UPDATE workspaces SET latest_successful_index=?,generation=generation+1 WHERE id=? AND generation=?', (index_id, self.id, job['base_generation']))
            c.execute("UPDATE refresh_attempts SET state='published',result_index_id=?,message='Full sealed analysis published',updated_at=CURRENT_TIMESTAMP WHERE id=?", (index_id, request_id))


def compilation_commands(raw: bytes, project: Path, roots: list[Path], namespace: dict, resolve) -> list[dict]:
    try:
        entries = json.loads(raw)
    except ValueError as error:
        raise IndexError('invalid_compilation_database', 'compilation database must be JSON') from error
    if not isinstance(entries, list) or not entries:
        raise IndexError('invalid_compilation_database', 'select at least one translation unit')
    commands = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get('directory'), str) or not isinstance(entry.get('file'), str):
            raise IndexError('invalid_compilation_database', 'each command needs file and directory strings')
        cwd = Path(entry['directory'])
        if not cwd.is_absolute():
            raise IndexError('invalid_compilation_database', 'command directory must be absolute')
        cwd = resolve(cwd)
        source = resolve(cwd/entry['file'])
        if not source.is_relative_to(project):
            raise IndexError('outside_project_root', 'translation unit is outside project')
        if namespace.get(str(source), {}).get('kind') != 'file':
            raise IndexError('stale_compilation_database', 'selected source is absent from captured inputs: '+str(source))
        args = _safe_arguments(_arguments(entry), source, cwd, roots, resolver=resolve)
        command = {'directory': str(cwd), 'file': str(source), 'arguments': args}
        if str(source) in commands and commands[str(source)] != command:
            raise IndexError('multiple_configurations', 'select one configuration per TU')
        commands[str(source)] = command
    return sorted(commands.values(), key=lambda c: c['file'])


def validate_evidence(collected) -> None:
    files, _, symbols, calls, *_ = collected
    for row in [*symbols.values(), *calls.values()]:
        for name in ('definition_range', 'normalized_definition', 'syntax'):
            evidence = row.get(name)
            if evidence is None:
                continue
            if not isinstance(evidence, dict) or evidence.get('file') not in files:
                raise IndexError('invalid_facts', 'syntax range lacks registered buffer')
            content, digest, _ = files[evidence['file']]
            begin, end = evidence.get('begin'), evidence.get('end')
            if content is None or type(begin) is not int or type(end) is not int or not 0 <= begin <= end <= len(content) or evidence.get('buffer_sha256') != digest or _sha256(content[begin:end]) != evidence.get('sha256'):
                raise IndexError('invalid_facts', 'definition/call range disagrees with indexed source')
            if not isinstance(evidence.get('token_sha256'), str) or len(evidence['token_sha256']) != 64:
                raise IndexError('invalid_facts', 'missing token evidence')


def bounded(value: dict, budget: int = 262_144) -> dict:
    if _status_size(value) > budget:
        raise IndexError('query_limit', 'query exceeds JSON byte budget; reduce page size')
    return value


def validate_id(value: str) -> None:
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError()
    except (ValueError, TypeError, AttributeError) as error:
        raise IndexError('invalid_arguments', 'request_id must be a canonical UUID') from error


def worker_limits() -> None:
    resource.setrlimit(resource.RLIMIT_CPU, (120, 120))
    resource.setrlimit(resource.RLIMIT_FSIZE, (268_435_456, 268_435_456))
    resource.setrlimit(resource.RLIMIT_AS, (4_294_967_296, 4_294_967_296))
