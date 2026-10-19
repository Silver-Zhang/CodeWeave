"""HTTP contract against real LibTooling facts, never a mock analyzer."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

from fastapi.testclient import TestClient
import pytest

from codeweave.app import create_app
from codeweave.index import query

ROOT = Path(__file__).resolve().parents[2]

@pytest.fixture(scope="module")
def indexed(tmp_path_factory):
    work = tmp_path_factory.mktemp("m1-http")
    source = work / "source"
    shutil.copytree(ROOT / "tests/fixtures/m1", source)
    left = source / 'left/unit.cpp'
    left.write_bytes(left.read_bytes().replace(b'\n', b'\r\n'))
    commands = [{"directory": str(source), "file": str(file), "arguments": ["c++", "-std=c++17", "-I", str(source), "-c", str(file)]} for file in sorted(source.rglob("*.cpp"))]
    (work / "compile_commands.json").write_text(json.dumps(commands))
    analyzer = Path(os.environ.get("CODEWEAVE_ANALYZER", ROOT / ".build/analyzer/codeweave-analyzer"))
    assert analyzer.is_file(), "M1 acceptance requires a real freshly built analyzer"
    database = work / "index.sqlite3"
    argv = [sys.executable, "-m", "codeweave", "index", "--project-root", str(source), "--compilation-database", str(work), "--database", str(database), "--analyzer", str(analyzer)]
    if os.environ.get("CODEWEAVE_RESOURCE_DIR"):
        argv += ["--resource-dir", os.environ["CODEWEAVE_RESOURCE_DIR"]]
    result = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    first = json.loads(result.stdout)["index_id"]
    (source / "left/unit.cpp").write_text("int changed_only() { return 99; }\n")
    result = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    second = json.loads(result.stdout)["index_id"]
    return database, source, first, second


def client(database):
    return TestClient(create_app(database), base_url="http://localhost")


def test_cli_http_equal_and_persistent_snapshots(indexed):
    database, source, first, second = indexed
    http = client(database)
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    assert http.get('/api/status').json()["index_id"] == second
    for endpoint, command, params, value in [
        ('status', 'status', {}, None), ('indexes', 'indexes', {}, None),
        ('search', 'search', {'q': 'over'}, 'over'), ('symbol', 'show', {'usr': 'c:@F@left#'}, 'c:@F@left#'),
        ('calls', 'callees', {'usr': 'c:@F@left#', 'direction': 'callees'}, 'c:@F@left#'),
        ('neighbors', 'neighbors', {'usr': 'c:@F@left#', 'limit': 12}, 'c:@F@left#'),
    ]:
        kwargs = {'index_id': first, **params}
        if command == 'indexes':
            kwargs = {'limit': 20}
        response = http.get('/api/' + endpoint, params=kwargs)
        assert response.status_code == 200, response.text
        limit = 12 if command == 'neighbors' else 20 if command in {'search', 'callees', 'indexes'} else 50
        expected = query(database, command, value, None if command == 'indexes' else first, limit=limit)
        assert response.json() == expected
    path = str(source / 'left/unit.cpp')
    params = {'index_id': first, 'path': path, 'start_line': 1, 'end_line': 5}
    response = http.get('/api/source', params=params)
    assert response.json() == query(database, 'source', path, first, start_line=1, end_line=5)
    assert 'int left()' in response.json()['content'] and 'changed_only' not in response.json()['content']
    assert client(database).get('/api/source', params=params).json() == response.json()
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before


def test_identity_calls_unknown_and_cycles(indexed):
    database, source, first, second = indexed
    http = client(database)
    def get(route, **params):
        response = http.get('/api/' + route, params={'index_id': first, **params})
        assert response.status_code == 200, response.text
        return response.json()
    over = get('search', q='over')['items']
    assert {s['usr'] for s in over} == {'c:@F@over#I#', 'c:@F@over#d#'}
    local = get('search', q='local')['items']
    assert len({s['usr'] for s in local}) == 2
    assert {s['definition_file'] for s in local} == {str(source / 'left/unit.cpp'), str(source / 'right/unit.cpp')}
    calls = get('calls', usr='c:@F@left#', direction='callees', limit=1)
    assert calls['returned_count'] == 1 and calls['total_count'] == 3 and calls['next_offset'] == 1
    pages = [get('calls', usr='c:@F@left#', direction='callees', limit=1, offset=i)['items'][0] for i in range(3)]
    assert len({c['instance_id'] for c in pages}) == 3
    dispatch = get('search', q='dispatch')['items'][0]
    graph = get('neighbors', usr=dispatch['usr'])
    assert {e['dispatch'] for e in graph['edges']} == {'virtual', 'unknown-function-pointer'}
    assert len([n for n in graph['nodes'] if n['kind'] == 'unknown']) == 1
    assert [e for e in graph['edges'] if e['dispatch'] == 'unknown-function-pointer'][0]['target_usr'] is None
    graph = get('neighbors', usr='c:@F@ping#I#')
    assert {n['usr'] for n in graph['nodes']} == {'c:@F@ping#I#', 'c:@F@pong#I#'}
    assert len(graph['edges']) == 2
    # left remains a shared-header declaration; its removed definition must disappear.
    removed = http.get('/api/symbol', params={'index_id': second, 'usr': 'c:@F@left#'}).json()['items'][0]
    assert removed['definition_file'] is None
    assert get('symbol', usr='c:@F@left#')['items'][0]['definition_file'] == str(source / 'left/unit.cpp')
    assert http.get('/api/symbol', params={'index_id': first, 'usr': 'c:@F@changed_only#'}).status_code == 404


@pytest.mark.parametrize('route,params,status,code', [
    ('search', {}, 422, 'invalid_arguments'),
    ('search', {'index_id':'missing'}, 404, 'index_not_found'),
    ('search', {'limit':0}, 400, 'invalid_limit'),
    ('search', {'offset':-1}, 400, 'invalid_offset'),
    ('symbol', {'usr':'missing'}, 404, 'symbol_not_found'),
    ('calls', {'usr':'missing','direction':'callees'}, 404, 'symbol_not_found'),
    ('calls', {'usr':'missing','direction':'wrong'}, 422, 'invalid_arguments'),
    ('source', {'path':'not-registered','start_line':1,'end_line':1}, 404, 'source_not_registered'),
    ('source', {'path':'not-registered','start_line':0,'end_line':1}, 422, 'invalid_arguments'),
    ('source', {'path':'not-registered','start_line':1,'end_line':201}, 413, 'source_limit'),
    ('neighbors', {'usr':'c:@F@left#','limit':51}, 400, 'invalid_arguments'),
])
def test_errors_are_json(indexed, route, params, status, code):
    database, _, first, _ = indexed
    data = {} if route == 'search' and not params else {'index_id':first}
    response = client(database).get('/api/'+route, params={**data, **params})
    assert response.status_code == status, response.text
    assert response.json()['error']['code'] == code
    assert 'Traceback' not in response.text


def test_database_boundary_and_negative_databases(indexed, tmp_path):
    database, _, first, _ = indexed
    http = client(database)
    # Even an existing, owned sentinel cannot be read through source or a DB override.
    sentinel = tmp_path / 'sentinel'; sentinel.write_text('OWNED-SENTINEL-NOT-SOURCE')
    response = http.get('/api/source', params={'index_id': first, 'path': str(sentinel), 'start_line':1,'end_line':1,'database':str(sentinel)})
    assert response.status_code == 404 and 'OWNED-SENTINEL' not in response.text
    assert http.get('/api/status', headers={'Origin':'https://example.invalid'}).status_code == 403
    assert http.get('/api/status', headers={'Host':'example.invalid'}).json()['error']['code'] == 'host_forbidden'
    assert http.get('/api/status', headers={'Origin':'http://localhost'}).status_code == 200
    for path, code in [(tmp_path/'missing','database_not_found'), (sentinel,'database_error')]:
        response = client(path).get('/api/status')
        assert response.json()['error']['code'] == code
    incompatible = tmp_path/'old.sqlite3'; sqlite3.connect(incompatible).close()
    assert client(incompatible).get('/api/status').json()['error']['code'] == 'schema_incompatible'
    assert TestClient(create_app(),base_url='http://localhost').get('/api/status').status_code == 503


def test_pagination_and_byte_limit(indexed, tmp_path):
    database, _, first, _ = indexed
    http = client(database)
    items = query(database,'search',index_id=first,limit=200)['items']
    actual = [http.get('/api/search',params={'index_id':first,'limit':1,'offset':i}).json()['items'][0] for i in range(len(items))]
    assert actual == items
    copy = tmp_path/'large.sqlite3'; shutil.copy2(database,copy)
    with sqlite3.connect(copy) as db:
        db.execute('UPDATE symbols SET anchors=? WHERE index_id=? AND usr=?', (json.dumps({'huge':'x'*270000}),first,'c:@F@left#'))
    response = client(copy).get('/api/symbol',params={'index_id':first,'usr':'c:@F@left#'})
    assert response.status_code == 413 and response.json()['error']['code'] == 'query_limit'


@pytest.mark.parametrize('name,line,prefix,definition_prefix', [
    ('unicode_entry', 6, 'int unicode_entry() { const char *s = u8"汉😀"; (void)s; return ', 'int '),
    ('han_entry', 7, 'int han_entry() { const char *s = u8"汉"; (void)s; return ', 'int '),
    ('emoji_entry', 8, 'int emoji_entry() { const char *s = u8"😀"; (void)s; return ', 'int '),
    ('ascii_entry', 9, 'int ascii_entry() { const char *s = "ascii"; (void)s; return ', 'int '),
    ('named_after_unicode', 10, '/*汉😀*/ int named_after_unicode() { return ', '/*汉😀*/ int '),
    ('combining_entry', 11, 'int combining_entry() { const char *s = u8"a\u0301"; (void)s; return ', 'int '),
    ('eol_entry', 12, '/*汉😀*/ int eol_entry() { return ', '/*汉😀*/ int '),
])
def test_unicode_anchors_remain_physical_byte_columns(indexed, name, line, prefix, definition_prefix):
    database, source, first, _ = indexed
    usr = 'c:@F@' + name + '#'
    http = client(database)
    response = http.get('/api/calls', params={'index_id': first, 'usr': usr, 'direction': 'callees'})
    assert response.status_code == 200
    calls = response.json()['items']
    assert len(calls) == 1
    assert (calls[0]['target_usr'], calls[0]['line'], calls[0]['column_number']) == ('c:@F@over#I#', line, len(prefix.encode('utf-8')) + 1)
    assert calls[0]['index_id'] == first
    entity = http.get('/api/symbol', params={'index_id': first, 'usr': usr}).json()['items'][0]
    assert (entity['definition_file'], entity['definition_line'], entity['definition_column']) == (str(source/'left/unit.cpp'), line, len(definition_prefix.encode('utf-8')) + 1)
