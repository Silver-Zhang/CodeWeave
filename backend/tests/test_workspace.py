"""M2-A acceptance: real Clang/CLI, controlled timing faults, immutable SQLite/HTTP.

No fact generators or fake successful analyzers. Mutations below are authored
before refresh; timing patches only change owned inputs or interrupt publication.
"""
import base64
import errno
import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import uuid

import pytest
from fastapi.testclient import TestClient

from codeweave.app import create_app
from codeweave.demo import create_demo
from codeweave.diff import compare, source_diff
from codeweave.index import IndexError, query
from codeweave.schema import migrate
from codeweave.workspace import Workspace, configure, writer
from codeweave import snapshot

ROOT=Path(__file__).resolve().parents[2]


def cli(*args, ok=True):
    p=subprocess.run([sys.executable,'-m','codeweave',*map(str,args)],capture_output=True,text=True,timeout=90)
    print(json.dumps({'argv':p.args,'exit':p.returncode,'stdout':p.stdout[:2500],'stderr':p.stderr[:2500]},ensure_ascii=False))
    assert p.returncode==(0 if ok else 2),(p.stdout,p.stderr)
    assert not (p.stderr if ok else p.stdout)
    return json.loads(p.stdout if ok else p.stderr)


@pytest.fixture
def demo(tmp_path):
    analyzer=os.environ.get('CODEWEAVE_ANALYZER'); resource=os.environ.get('CODEWEAVE_RESOURCE_DIR')
    assert analyzer and resource,'M2 acceptance requires a freshly built analyzer and resource directory; no skip fallback'
    project=create_demo(tmp_path/'project',ROOT/'examples/workspace')
    state=tmp_path/'state'
    configure(state,project,project,Path(analyzer),Path(resource))
    return project,Workspace(state)


def refresh(ws, ok=True, kind='workspace'):
    result=cli('workspace','head' if kind=='head' else 'refresh','--state-dir',ws.state,ok=ok)
    if ok:
        assert result['state']=='published',result
        return result['result_index_id']
    return result


def current(ws):return ws.status()['latest_successful_index']
def symbols(ws,index):return query(ws.database,'search','',index,limit=200)['items']
def named(ws,index,name):return next(s for s in symbols(ws,index) if s['name']==name)
def content(ws,index,path):return query(ws.database,'source',str(path),index,start_line=1,end_line=200,clamp_end=True)['content']
def changes(ws,a,b,group):return compare(ws.database,a,b,group,200)['items']


def test_public_vertical_git_old_source_lines_macro_warning_and_restart(demo):
    p,ws=demo
    old=refresh(ws);old_main=content(ws,old,p/'main.cpp')
    assert 'return 7;' in old_main
    assert query(ws.database,'status',index_id=old)['represented_git_commit']
    (p/'main.cpp').write_text(old_main.replace('return 7;','return 9;').replace('return alpha();','return beta();'))
    new=refresh(ws)
    delta=changes(ws,old,new,'symbols')
    assert {x['after']['name'] for x in delta if x['kind']=='definition_text_changed'}=={'demo::literal','demo::choose'}
    calls=changes(ws,old,new,'calls');target=[x for x in calls if x['kind']=='target_changed']
    assert len(target)==1 and target[0]['before']['target_usr'].endswith('alpha#') and target[0]['after']['target_usr'].endswith('beta#')
    assert {x['kind'] for x in calls}=={'target_changed','relation_added','relation_removed'}
    assert content(ws,old,p/'main.cpp')==old_main
    assert query(ws.database,'status',index_id=new)['represented_git_commit'] is None
    head=refresh(ws,kind='head')
    assert content(ws,head,p/'main.cpp')==old_main and current(ws)==new
    (p/'main.cpp').write_text('\n\n'+(p/'main.cpp').read_text())
    moved=refresh(ws)
    assert all(x['kind']=='location_changed' for x in changes(ws,new,moved,'symbols'))
    assert all(x['kind'] in {'site_location_changed','ambiguous_correspondence'} for x in changes(ws,new,moved,'calls'))
    config_before=hashlib.sha256((p/'config.hpp').read_bytes()).hexdigest()
    (p/'config.hpp').write_text('#pragma once\n#define DEMO_SCALE 5\n')
    macro=refresh(ws)
    context=next(x for x in changes(ws,moved,macro,'symbols') if x['kind']=='definition_context_changed' and x['after']['name']=='demo::scaled')
    assert {'kind':'input','path':str(p/'config.hpp'),'before_sha256':config_before,
            'after_sha256':hashlib.sha256((p/'config.hpp').read_bytes()).hexdigest()} in context['changed_inputs']
    (p/'main.cpp').write_text('#warning owned-warning\n'+(p/'main.cpp').read_text())
    warned=refresh(ws)
    assert query(ws.database,'status',index_id=warned)['diagnostic_summary']['warning']>=1
    opened=cli('open','--state-dir',ws.state,'--frontend',ROOT/'frontend','--no-serve')
    assert opened['latest_successful_index']==warned and Workspace(ws.state).status()['generation']==5
    diff=source_diff(ws.database,old,new,str(p/'main.cpp'),str(p/'main.cpp'))
    assert 'return 7;' in diff['left']['content'] and 'return 9;' in diff['right']['content']


def test_shared_inline_template_and_identity_decl_scope_removed(demo):
    p,ws=demo
    (p/'shared.hpp').write_text((p/'shared.hpp').read_text()+ '\nstatic int local_header(){return 1;}\ntemplate<class T> T twice(T v){return v+v;}\n')
    for folder,name in [('left','l'),('right','r')]:
        (p/folder).mkdir();(p/folder/'unit.cpp').write_text('#include "shared.hpp"\nstatic int same(){return local_header();}\nint '+name+'(){return same()+twice(1)+demo::scaled(1);}\n')
    commands=json.loads((p/'compile_commands.json').read_text())
    commands += [{'directory':str(p),'file':str(p/d/'unit.cpp'),'arguments':['c++','-std=c++17','-I',str(p),'-c',str(p/d/'unit.cpp')]} for d in ('left','right')]
    (p/'compile_commands.json').write_text(json.dumps(commands[::-1]))
    a=refresh(ws);rows=symbols(ws,a)
    same=[x for x in rows if x['name']=='same'];internal=[x for x in rows if x['name']=='local_header']
    assert len(same)==2 and len({x['usr'] for x in same})==2
    assert {Path(x['definition_file']).parent.name for x in same}=={'left','right'}
    assert len(internal)==4 and len({x['usr'] for x in internal})==4
    assert len([x for x in rows if x['name']=='demo::scaled'])==1
    assert any(x['name'].startswith('twice') and x['definition_file'] for x in rows)
    (p/'compile_commands.json').write_text(json.dumps(commands+commands[:1]))
    b=refresh(ws)
    assert not changes(ws,a,b,'calls')
    assert len(symbols(ws,a))==len(symbols(ws,b))
    # Drop sole definition TU, but keep its declarations from shared.hpp.
    (p/'compile_commands.json').write_text(json.dumps([c for c in commands if not c['file'].endswith('worker.cpp')]))
    c=refresh(ws)
    alpha=named(ws,c,'demo::alpha');assert alpha['definition_file'] is None
    removed=[x for x in changes(ws,b,c,'symbols') if x['kind']=='definition_removed']
    assert any(x['after']['name']=='demo::alpha' for x in removed)
    assert not any(x['kind']=='symbol_removed' and x['before']['name']=='demo::alpha' for x in changes(ws,b,c,'symbols'))
    assert any(x['kind']=='relation_out_of_scope' for x in changes(ws,b,c,'calls'))
    (p/'unlisted.cpp').write_text('int unlisted(){return 4;}\n')
    d=refresh(ws);assert query(ws.database,'status',index_id=d)['uncovered_file_count']==2
    (p/'left/unit.cpp').unlink()
    error=refresh(ws,False);assert error['error']['code']=='stale_compilation_database' and current(ws)==d


def test_repeated_sites_remain_ambiguous_and_real_call_semantics(demo):
    p,ws=demo
    code='''#include "shared.hpp"
#define PAIR(x) x(); x()
int repeated(){ PAIR(demo::alpha); return demo::alpha()+demo::alpha(); }
int repeated_plain(){return demo::alpha()+demo::alpha();}
int semantic(){ auto p=&demo::alpha; auto l=[](){ auto inner=[](){return demo::alpha();}; return inner();}; (void)sizeof(demo::alpha()); return p()+l(); }
int virtuals(demo::Reader& r){return r.dispatch()+r.demo::Reader::dispatch();}
'''
    (p/'main.cpp').write_text(code)
    a=refresh(ws);rep=named(ws,a,'repeated');calls=query(ws.database,'callees',rep['usr'],a)['items']
    assert len(calls)==4 and len({c['instance_id'] for c in calls})==4
    sem=named(ws,a,'semantic');sc=query(ws.database,'callees',sem['usr'],a)['items']
    assert any(c['target_usr'] is None for c in sc)
    assert not any(c['target_usr']=='c:@N@demo@F@alpha#' for c in sc)
    v=query(ws.database,'callees',named(ws,a,'virtuals')['usr'],a)['items']
    assert {x['dispatch'] for x in v}=={'virtual','direct'}
    (p/'main.cpp').write_text('\n'+code.replace('PAIR(demo::alpha)','PAIR(demo::beta)').replace('return demo::alpha()+demo::alpha();','return demo::beta()+demo::beta();'))
    b=refresh(ws);delta=changes(ws,a,b,'calls')
    assert not any(x['kind']=='target_changed' and (x['before'] or {}).get('caller_usr')==rep['usr'] for x in delta)
    assert any(x['kind'] in {'ambiguous_correspondence','unmatched_sites'} and (x['before'] or {}).get('caller_usr')==rep['usr'] for x in delta)
    plain=named(ws,a,'repeated_plain')
    assert any(x['kind']=='ambiguous_correspondence' and (x['before'] or {}).get('caller_usr')==plain['usr'] for x in delta)
    assert {x['before_count'] for x in delta if x['kind']=='relation_removed' and x['caller_usr']==rep['usr']}=={4}


def test_capture_barriers_host_changes_and_sealed_tamper(demo,monkeypatch):
    p,ws=demo
    old=refresh(ws);original=(p/'config.hpp').read_text();realstep=ws.step
    def after_seal(request,state):
        realstep(request,state)
        if state=='sealed':
            (p/'config.hpp').write_text(original.replace('2','8'))
            (p/'main.cpp').write_text((p/'main.cpp').read_text().replace('return 7','return 6'))
    monkeypatch.setattr(ws,'step',after_seal)
    job=ws.enqueue(ws.status()['generation'],str(uuid.uuid4()));result=ws.run(job['request_id'])
    assert result['state']=='published',result
    new=result['result_index_id']
    assert content(ws,new,p/'config.hpp')==original
    assert 'return 7;' in content(ws,new,p/'main.cpp')
    assert query(ws.database,'status',index_id=new)['workspace_changed_since_capture'] is True
    monkeypatch.setattr(ws,'step',realstep)
    capture=snapshot.capture
    def tamper(*args):
        manifests,mounts=capture(*args); (mounts[0][0]/'injected.hpp').write_text('// tamper');return manifests,mounts
    monkeypatch.setattr(snapshot,'capture',tamper)
    job=ws.enqueue(ws.status()['generation'],str(uuid.uuid4()));bad=ws.run(job['request_id'])
    assert bad['state']=='failed' and bad['error_code']=='sealed_input_changed' and current(ws)==new
    assert content(ws,old,p/'config.hpp')==original


@pytest.mark.parametrize('changed',['main.cpp','shared.hpp','config.hpp','compile_commands.json'])
def test_capture_mutation_retries_once_and_fails_without_publication(demo,monkeypatch,changed):
    p,ws=demo;old=refresh(ws);inventory=snapshot.inventory;attempts=[]
    def racing(root,destination=None,**kwargs):
        rows=inventory(root,destination,**kwargs)
        if root==p and destination is not None:
            attempts.append(1)
            path=p/changed
            if changed.endswith('.json'):
                path.write_text(path.read_text()+' ')
            else:
                path.write_text(path.read_text()+'\n// owned mutation after copy\n')
        return rows
    monkeypatch.setattr(snapshot,'inventory',racing)
    job=ws.enqueue(ws.status()['generation'],str(uuid.uuid4()));bad=ws.run(job['request_id'])
    assert len(attempts)==2 and bad['error_code']=='workspace_changed_during_capture' and current(ws)==old


@pytest.mark.parametrize('bad_input',['frontend','missing_header','driver','response'])
def test_analysis_failure_preserves_old_and_exposes_diagnostics(demo,bad_input):
    p,ws=demo;old=refresh(ws)
    if bad_input=='frontend':(p/'worker.cpp').write_text('int incomplete( {\n')
    elif bad_input=='missing_header':(p/'worker.cpp').write_text('#include "not_present.hpp"\n')
    else:
        commands=json.loads((p/'compile_commands.json').read_text());commands[0]['arguments'][1:1]=['-D','@owned.rsp'] if bad_input=='response' else ['-invalid-driver-switch']
        (p/'compile_commands.json').write_text(json.dumps(commands));(p/'owned.rsp').write_text('-DCHANGED=1')
    failure=refresh(ws,False)
    assert current(ws)==old and 'return 7;' in content(ws,old,p/'main.cpp')
    attempt=ws.status()['last_attempt'];assert attempt['state']=='failed'
    if bad_input in {'frontend','missing_header'}:
        units=cli('workspace','attempt',attempt['request_id'],'--state-dir',ws.state,'--details','units')
        assert units['unit_count']==2 and any(x['state']=='failed' and x['diagnostics']!='[]' for x in units['items'])
    else:assert failure['error']['code']=='unsafe_compilation_argument'


def test_publication_cancel_enospc_constraint_and_interrupted_recovery(demo,monkeypatch):
    p,ws=demo;old=refresh(ws);realstep=ws.step
    for failure in ('cancel','enospc','constraint'):
        def fault(request,state):
            realstep(request,state)
            if state=='publishing':
                if failure=='cancel':ws.cancel(request)
        with monkeypatch.context() as m:
            m.setattr(ws,'step',fault)
            if failure in {'constraint','enospc'}:
                from codeweave import workspace
                insert=workspace.insert_facts
                def failed_insert(c,*args):
                    insert(c,*args)
                    if failure=='enospc':raise OSError(errno.ENOSPC,'owned disk-full fault inside publication transaction')
                    c.execute('INSERT INTO workspaces(id,config_identity,generation) VALUES (?,?,0)',(ws.id,'duplicate'))
                m.setattr(workspace,'insert_facts',failed_insert)
            job=ws.enqueue(ws.status()['generation'],str(uuid.uuid4()));result=ws.run(job['request_id'])
            assert result['state'] in {'failed','cancelled'} and current(ws)==old
            assert len(query(ws.database,'indexes')['items'])==1
    # A killed owner leaves no new facts/default and is marked interrupted on restart.
    job=ws.enqueue(ws.status()['generation'],str(uuid.uuid4()))
    pid=os.fork()
    if pid==0:
        def stop(request,state):
            realstep(request,state)
            if state=='publishing':os.kill(os.getpid(),signal.SIGKILL)
        ws.step=stop;ws.run(job['request_id']);os._exit(9)
    os.waitpid(pid,0)
    reopened=Workspace(ws.state);reopened.recover()
    assert reopened.attempt(job['request_id'])['state']=='interrupted' and current(ws)==old
    assert 'return 7;' in content(ws,old,p/'main.cpp')
    # Kill after commit: complete facts/default/attempt must survive together.
    committed=ws.enqueue(ws.status()['generation'],str(uuid.uuid4()))
    pid=os.fork()
    if pid==0:
        ws.run(committed['request_id']);os.kill(os.getpid(),signal.SIGKILL)
    os.waitpid(pid,0)
    reopened.recover();published=reopened.attempt(committed['request_id'])
    assert published['state']=='published' and current(ws)==published['result_index_id']
    assert 'return 7;' in content(ws,current(ws),p/'main.cpp')


def test_namespace_optional_header_and_equal_metadata(demo):
    p,ws=demo
    code='#include "shared.hpp"\n#if __has_include("optional.hpp")\n#include "optional.hpp"\n#else\n#define PICK demo::alpha\n#endif\nint choose_optional(){return PICK();}\n'
    (p/'main.cpp').write_text(code);a=refresh(ws)
    (p/'optional.hpp').write_text('#define PICK demo::beta \n');b=refresh(ws)
    assert any(x['kind']=='relation_added' and x['target_usr'].endswith('beta#') for x in changes(ws,a,b,'calls'))
    f=p/'optional.hpp';st=f.stat();f.write_text('#define PICK demo::alpha\n')
    os.utime(f,ns=(st.st_atime_ns,st.st_mtime_ns));c=refresh(ws)
    assert any(x['kind']=='relation_added' and x['target_usr'].endswith('alpha#') for x in changes(ws,b,c,'calls'))


def test_security_paths_line_git_and_response_forms(demo):
    p,ws=demo;sentinel=p.parent/'sentinel';sentinel.write_text('private owned sentinel')
    # Hostile Git hooks/config never execute; raw object reads keep imported index unchanged.
    hook=p/'.git/hooks/post-index-change';hook.write_text('#!/bin/sh\necho leaked > '+str(sentinel)+'\n');hook.chmod(0o755)
    subprocess.run(['git','-C',str(p),'config','core.fsmonitor','echo leaked > '+str(sentinel)],check=True)
    idx=(p/'.git/index').read_bytes();a=refresh(ws)
    assert (p/'.git/index').read_bytes()==idx and sentinel.read_text()=='private owned sentinel'
    (p/'main.cpp').write_text('#line 12 "'+str(sentinel)+'"\nint owned(){return 1;}\n')
    b=refresh(ws);entity=named(ws,b,'owned');assert entity['definition_file']==str(p/'main.cpp')
    with pytest.raises(IndexError,match='not registered'):query(ws.database,'source',str(sentinel),b,start_line=1,end_line=1)
    (p/'escape.hpp').symlink_to(sentinel)
    assert refresh(ws,False)['error']['code']=='unsupported_snapshot_link'
    (p/'escape.hpp').unlink();os.mkfifo(p/'fifo')
    assert refresh(ws,False)['error']['code']=='unsupported_input'
    assert current(ws)==b and sentinel.read_text()=='private owned sentinel'


def test_http_contract_readonly_tokens_cas_idempotency_and_chunks(demo,monkeypatch):
    p,ws=demo;old=refresh(ws)
    readonly=TestClient(create_app(ws.database,workspace_state=ws.state),base_url='http://127.0.0.1')
    assert readonly.post('/api/workspace/refresh',json={}).status_code==403
    client=TestClient(create_app(workspace_state=ws.state,allow_workspace_refresh=True),base_url='http://127.0.0.1')
    token=client.get('/api/session').json()['csrf_token'];headers={'X-CodeWeave-Token':token}
    body={'generation':1,'idempotency_token':'owned-token','source_kind':'workspace'}
    assert client.post('/api/workspace/refresh',json=body).status_code==403
    assert client.post('/api/workspace/refresh',json=body,headers={**headers,'Origin':'https://evil.example'}).status_code==403
    assert client.post('/api/workspace/refresh',json=body,headers={**headers,'Host':'evil.example'}).status_code==400
    assert client.post('/api/workspace/refresh',content='{}',headers=headers).status_code==400
    for bad in ({**body,'project_root':str(p)},{**body,'source_kind':[]},{**body,'generation':True}):
        assert client.post('/api/workspace/refresh',json=bad,headers=headers).status_code in {400,422}
    assert client.post('/api/workspace/refresh',json={**body,'generation':0},headers=headers).status_code==409
    # Only suppress process spawn; no successful facts synthesized. Explicit real run follows.
    with monkeypatch.context() as m:
        m.setattr(Workspace,'spawn',lambda *args:None)
        job=client.post('/api/workspace/refresh',json=body,headers=headers);assert job.status_code==202
        duplicate=client.post('/api/workspace/refresh',json=body,headers=headers)
        assert duplicate.json()['request_id']==job.json()['request_id']
        assert client.post('/api/workspace/refresh',json={**body,'idempotency_token':'other'},headers=headers).status_code==409
    result=ws.run(job.json()['request_id']);assert result['state']=='published'
    assert client.get('/api/symbol',params={'index_id':old,'usr':'unknown'}).status_code==404
    assert client.get('/api/diff',params={'left':'missing','right':old}).status_code==400 or client.get('/api/diff',params={'left':'missing','right':old}).status_code==404
    field=client.get('/api/workspace/attempts/'+job.json()['request_id'],params={'details':'units','field':'command','limit':1}).json()
    raw=base64.b64decode(field['content']);assert hashlib.sha256(raw).hexdigest()==field['field_sha256']
    assert client.get('/api/source',params={'index_id':old,'path':str(p/'main.cpp'),'start_line':1,'end_line':1000}).status_code==413
    assert current(ws)==result['result_index_id']


def test_schema3_migration_readonly_and_comparison_bounds(demo,tmp_path):
    p,ws=demo
    legacy=tmp_path/'legacy.sqlite3'
    cli('index','--project-root',p,'--compilation-database',p,'--database',legacy,'--analyzer',ws.config['analyzer'],'--resource-dir',ws.config['resource_dir'])
    before=hashlib.sha256(legacy.read_bytes()).hexdigest();old=query(legacy,'status')['index_id']
    destination=tmp_path/'migrated.sqlite3';cli('migrate','--from',legacy,'--to',destination)
    assert hashlib.sha256(legacy.read_bytes()).hexdigest()==before
    assert content(type('DB',(),{'database':destination}),old,p/'main.cpp')==content(type('DB',(),{'database':legacy}),old,p/'main.cpp')
    with pytest.raises(IndexError) as error:compare(destination,old,old)
    assert error.value.code=='not_comparable_legacy_index'
    assert cli('migrate','--from',legacy,'--to',destination,ok=False)['error']['code']=='destination_exists'
    a=refresh(ws)
    # 300 real functions changed: public pagination is complete/stable and bounded.
    extra=p/'main.cpp';extra.write_text(extra.read_text()+'\n'+'\n'.join(f'int added_{i}(){{return {i};}}' for i in range(300)))
    b=refresh(ws);seen=[];offset=0
    while True:
        page=compare(ws.database,a,b,'symbols',1,offset,'symbol_added');assert page['returned_count']==1
        assert len(json.dumps(page).encode())<262144
        seen.append(page['items'][0]['event_id'])
        if not page['has_more']:break
        assert page['next_offset']>offset;offset=page['next_offset']
    assert len(set(seen))==300
    assert len(json.dumps(compare(ws.database,a,b)).encode())<32768


def test_missing_head_inputs_block_only_head_and_staged_saved_provenance(demo):
    p,ws=demo
    # Generated header is saved but never committed. HEAD must not borrow it.
    (p/'generated.hpp').write_text('#define GENERATED 3\n')
    (p/'main.cpp').write_text('#include "generated.hpp"\nint generated(){return GENERATED;}\n')
    subprocess.run(['git','-C',str(p),'add','--','main.cpp'],check=True)
    subprocess.run(['git','-C',str(p),'-c','core.hooksPath=/dev/null','commit','-m','Own missing historical input'],check=True,capture_output=True)
    (p/'main.cpp').write_text('#include "generated.hpp"\nint generated(){return GENERATED+1;}\n')
    subprocess.run(['git','-C',str(p),'add','--','main.cpp'],check=True)
    (p/'main.cpp').write_text('#include "generated.hpp"\nint generated(){return GENERATED+2;}\n')
    a=refresh(ws)
    assert 'GENERATED+2' in content(ws,a,p/'main.cpp')
    with sqlite3.connect(ws.database) as c:
        provenance=json.loads(c.execute('SELECT provenance FROM index_metadata WHERE index_id=?',(a,)).fetchone()[0])
    assert 'main.cpp' in provenance['staged_paths'] and 'main.cpp' in provenance['unstaged_paths'] and 'generated.hpp' in provenance['untracked_paths']
    error=refresh(ws,False,kind='head')
    assert error['error']['code']=='analysis_failed' and current(ws)==a
    assert 'GENERATED+2' in content(ws,a,p/'main.cpp')


def test_attempt_long_utf8_chunking_and_schema_rejection(demo):
    p,ws=demo;job=ws.enqueue(0,'owned-long-fields');text='诊断😀'*5000
    # D-only metadata fixture; no successful analyzer facts are synthesized.
    with writer(ws.database) as c:
        c.execute("UPDATE refresh_attempts SET state='failed',message=?,error_code='owned' WHERE id=?",(text,job['request_id']))
        c.execute('INSERT INTO attempt_units VALUES (?,?,?,?,?)',(job['request_id'],str(p/'main.cpp'),'failed',json.dumps([text],ensure_ascii=False),text))
    summary=ws.status();assert len(json.dumps(summary,ensure_ascii=False).encode())<32768
    details=ws.attempt(job['request_id'],details=True,limit=1);assert details['returned_count']==1
    assert details['items'][0]['command']['truncated']
    raw=b'';offset=0
    while True:
        chunk=ws.attempt(job['request_id'],1,0,True,'command',offset)
        raw+=base64.b64decode(chunk['content']);assert len(json.dumps(chunk,ensure_ascii=False).encode())<32768
        if not chunk['has_more']:break
        assert chunk['next_field_offset']>offset;offset=chunk['next_field_offset']
    assert raw==text.encode() and hashlib.sha256(raw).hexdigest()==chunk['field_sha256']
    assert cli('workspace','attempt','bad','--state-dir',ws.state,ok=False)['error']['code']=='invalid_arguments'
    with writer(ws.database) as c:c.execute('PRAGMA user_version=99')
    assert cli('workspace','status','--state-dir',ws.state,ok=False)['error']['code']=='schema_incompatible'


@pytest.mark.parametrize('body',[
    'auto l=[](){auto nested=[](){return demo::alpha();};return nested();};return l();',
    'struct C{C(){demo::alpha();}~C(){demo::beta();} static int s(){return demo::alpha();} int m(){return s();}};C c;return c.m();',
])
def test_nested_lambda_line_only_preserves_entity_and_relation_identity(demo,body):
    p,ws=demo
    code='#include "shared.hpp"\nint entry(){'+body+'}\n'
    (p/'main.cpp').write_text(code);a=refresh(ws)
    old={r['usr'] for r in symbols(ws,a)}
    (p/'main.cpp').write_text('\n\n'+code);b=refresh(ws)
    assert {r['usr'] for r in symbols(ws,b)}==old
    assert all(x['kind']=='location_changed' for x in changes(ws,a,b,'symbols'))
    assert all(x['kind'] in {'site_location_changed','ambiguous_correspondence'} for x in changes(ws,a,b,'calls'))


def test_selected_add_rename_delete_and_configuration_profiles(demo):
    p,ws=demo;a=refresh(ws)
    unit=p/'added.cpp';unit.write_text('static int private_name(){return 4;}\nint added(){return private_name();}\n')
    commands=json.loads((p/'compile_commands.json').read_text())
    command={'directory':str(p),'file':'added.cpp','arguments':['c++','-std=c++17','-D','OWNED=1','-I',str(p),'-c','added.cpp']}
    (p/'compile_commands.json').write_text(json.dumps([*commands,command]));b=refresh(ws)
    assert any(x['kind']=='symbol_added' and x['after']['name']=='added' for x in changes(ws,a,b,'symbols'))
    old_private=named(ws,b,'private_name')['usr'];external=named(ws,b,'added')['usr']
    unit.rename(p/'renamed.cpp');command['file']='renamed.cpp';command['arguments'][-1]='renamed.cpp'
    (p/'compile_commands.json').write_text(json.dumps([*commands,command]));c=refresh(ws)
    assert named(ws,c,'private_name')['usr']!=old_private and named(ws,c,'added')['usr']==external
    (p/'renamed.cpp').unlink();(p/'compile_commands.json').write_text(json.dumps(commands));d=refresh(ws)
    assert any(x['kind']=='symbol_removed' and x['before']['name']=='added' for x in changes(ws,c,d,'symbols'))
    different=json.loads(json.dumps(commands));different[0]['arguments'].insert(1,'-DOWNED=2')
    (p/'compile_commands.json').write_text(json.dumps(different));e=refresh(ws)
    assert compare(ws.database,d,e)['configuration_changed']
    (p/'compile_commands.json').write_text(json.dumps(commands+different))
    assert refresh(ws,False)['error']['code']=='multiple_configurations' and current(ws)==e


def test_readset_constexpr_enum_context_with_hashes_and_real_cli_http(demo):
    p,ws=demo
    header=p/'settings.hpp'
    header.write_text('#pragma once\ninline constexpr unsigned scale=2;\nenum class Level:unsigned { value=3 };\n')
    for name,body in [('a','#include "settings.hpp"\nunsigned context_a(){return scale*static_cast<unsigned>(Level::value);}\n'),
                      ('b','#include "settings.hpp"\nunsigned context_b(){return scale;}\n'),
                      ('c','unsigned context_c(){return 9;}\n')]:
        (p/(name+'.cpp')).write_text(body)
    commands=[{'directory':str(p),'file':str(p/(name+'.cpp')),
               'arguments':['c++','-std=c++17','-c',str(p/(name+'.cpp'))]} for name in ('a','b','c')]
    (p/'compile_commands.json').write_text(json.dumps(commands))
    old=refresh(ws);before=hashlib.sha256(header.read_bytes()).hexdigest()
    header.write_text('#pragma once\ninline constexpr unsigned scale=4;\nenum class Level:unsigned { value=5 };\n')
    new=refresh(ws);after=hashlib.sha256(header.read_bytes()).hexdigest()
    assert query(ws.database,'status',index_id=old)['profile_id']==query(ws.database,'status',index_id=new)['profile_id']
    expected={'kind':'input','path':str(header),'before_sha256':before,'after_sha256':after}
    delta=changes(ws,old,new,'symbols')
    assert {(x['kind'],x['after']['name']) for x in delta}=={
        ('definition_context_changed','context_a'),('definition_context_changed','context_b')}
    for item in delta:
        assert item['before']['definition_range']['sha256']==item['after']['definition_range']['sha256']
        assert item['odr_hint']['before']==item['odr_hint']['after'] and not item['odr_hint']['authoritative']
        assert item['changed_inputs']==[expected] and item['changed_input_count']==1
        assert not item['changed_inputs_truncated']
    assert not changes(ws,old,new,'calls')
    first=cli('query','diff-symbols','--database',ws.database,'--left',old,'--right',new,'--limit',1)
    second=cli('query','diff-symbols','--database',ws.database,'--left',old,'--right',new,'--limit',1,'--offset',first['next_offset'])
    assert first['returned_count']==second['returned_count']==1 and first['total_count']==2
    assert {first['items'][0]['after']['name'],second['items'][0]['after']['name']}=={'context_a','context_b'}
    response=TestClient(create_app(ws.database),base_url='http://127.0.0.1').get('/api/diff/symbols',params={'left':old,'right':new,'limit':1})
    assert response.status_code==200 and response.json()==first
    # A configuration change supplies its exact old/new identity as well.
    commands[0]['arguments'].insert(1,'-DCONTEXT_PROFILE=1')
    (p/'compile_commands.json').write_text(json.dumps(commands));configured=refresh(ws)
    profile=changes(ws,new,configured,'symbols')
    assert compare(ws.database,new,configured)['configuration_changed']
    assert profile and all(any(c['kind']=='profile' and c['before']!=c['after'] for c in e['changed_inputs']) for e in profile)


def test_same_file_external_constant_stays_visible_as_file_delta(demo):
    """Document the coarse context boundary without declaring runtime equivalence."""
    p,ws=demo
    source=p/'main.cpp'
    before='constexpr int own_value=1;\nint same_file(){return own_value;}\n'
    source.write_text(before)
    commands=[{'directory':str(p),'file':str(source),'arguments':['c++','-std=c++17','-c',str(source)]}]
    (p/'compile_commands.json').write_text(json.dumps(commands))
    old=refresh(ws);source.write_text(before.replace('own_value=1','own_value=2'));new=refresh(ws)
    assert content(ws,old,source)==before and 'own_value=2' in content(ws,new,source)
    assert [(x['kind'],x['after']['file']) for x in changes(ws,old,new,'files')]==[('file_text_changed',str(source))]
    assert not any(x['kind']=='definition_text_changed' for x in changes(ws,old,new,'symbols'))
    assert not changes(ws,old,new,'calls')


def test_context_preview_byte_budget_keeps_full_readset_evidence(demo):
    p,ws=demo;folder=p/('层'*70)/('目'*70);folder.mkdir(parents=True)
    headers=[folder/('配'*70+f'{i:02}.hpp') for i in range(12)]
    for i,path in enumerate(headers):path.write_text(f'inline constexpr int input_{i}=1;\n')
    (p/'main.cpp').write_text(''.join('#include "'+path.relative_to(p).as_posix()+'"\n' for path in headers)+
                              'int read_many(){return '+'+'.join('input_'+str(i) for i in range(12))+';}\n')
    (p/'compile_commands.json').write_text(json.dumps([{'directory':str(p),'file':str(p/'main.cpp'),
        'arguments':['c++','-std=c++17','-c',str(p/'main.cpp')]}]))
    old_hashes={str(path):hashlib.sha256(path.read_bytes()).hexdigest() for path in headers}
    old=refresh(ws)
    for i,path in enumerate(headers):path.write_text(f'inline constexpr int input_{i}=2;\n')
    expected=[{'kind':'input','path':str(path),'before_sha256':old_hashes[str(path)],
               'after_sha256':hashlib.sha256(path.read_bytes()).hexdigest()} for path in sorted(headers)]
    new=refresh(ws);page=compare(ws.database,old,new,'symbols',1);event=page['items'][0]
    assert page['total_count']==page['returned_count']==1 and event['kind']=='definition_context_changed'
    assert event['changed_input_count']==12 and event['changed_inputs_truncated']
    assert 0<len(event['changed_inputs'])<8 and event['changed_inputs']==expected[:len(event['changed_inputs'])]
    assert len(json.dumps(event['changed_inputs'],sort_keys=True,separators=(',',':')).encode())<=8192
    full=json.dumps(expected,sort_keys=True,separators=(',',':')).encode()
    assert event['changed_inputs_hash_format']=='json-sort-keys-compact-ascii-v1'
    assert event['changed_inputs_sha256']==hashlib.sha256(full).hexdigest()
    assert len(json.dumps(page,ensure_ascii=False).encode())<=262144
    with sqlite3.connect(ws.database) as c:
        for index in (old,new):
            inputs=c.execute('SELECT u.path,f.sha256 FROM unit_files u JOIN files f ON f.index_id=u.index_id AND f.path=u.path WHERE u.index_id=? AND u.source_file=?',(index,str(p/'main.cpp'))).fetchall()
            assert all(str(path) in dict(inputs) for path in headers)


def test_source_hash_windows_utf8_failure_and_integrity_cli_http(demo):
    p,ws=demo;source=p/'main.cpp'
    raw='#include "shared.hpp"\r\n// 中文🙂e\u0301\r\nint main(){return demo::alpha();}\r\n'.encode()
    source.write_bytes(raw);old=refresh(ws)
    full=cli('query','source',source,'--database',ws.database,'--index-id',old,'--start-line',1,'--end-line',3)
    assert full['content'].encode()==raw and full['encoding']=='utf-8'
    assert full['sha256']==full['buffer_sha256']==full['range_sha256']==hashlib.sha256(raw).hexdigest()
    assert (full['begin'],full['end'])==(0,len(raw))
    client=TestClient(create_app(ws.database),base_url='http://127.0.0.1');params={'index_id':old,'path':str(source),'start_line':2,'end_line':3}
    window=client.get('/api/source',params=params)
    assert window.status_code==200
    snippet=window.json();offset=len(raw.splitlines(keepends=True)[0]);expected=raw[offset:]
    assert snippet['content'].encode()==expected and (snippet['begin'],snippet['end'])==(offset,len(raw))
    assert snippet['buffer_sha256']==hashlib.sha256(raw).hexdigest() and snippet['range_sha256']==hashlib.sha256(expected).hexdigest()
    changed=raw.replace(b'alpha()',b'beta()');source.write_bytes(changed);new=refresh(ws)
    pair=cli('query','diff-source','--database',ws.database,'--left',old,'--right',new,
             '--left-path',source,'--right-path',source,'--lines',3)
    for side,data in [('left',raw),('right',changed)]:
        assert pair[side]['content'].encode()==data
        assert pair[side]['buffer_sha256']==pair[side]['range_sha256']==hashlib.sha256(data).hexdigest()
    bad=b'// invalid \xff owned\r\nint main(){return 0;}\r\n'
    source.write_bytes(bad);invalid=refresh(ws)
    error=cli('query','source',source,'--database',ws.database,'--index-id',invalid,'--start-line',1,'--end-line',2,ok=False)
    assert error['error']['code']=='source_not_utf8' and 'content' not in error
    failed=client.get('/api/source',params={**params,'index_id':invalid,'start_line':1,'end_line':2})
    assert failed.status_code==422 and failed.json()['error']['code']=='source_not_utf8'
    pair_failure=client.get('/api/source-diff',params={'left':old,'right':invalid,'left_path':str(source),'right_path':str(source),'lines':3})
    assert pair_failure.status_code==422 and pair_failure.json()['error']['code']=='source_not_utf8'
    valid_line=client.get('/api/source',params={**params,'index_id':invalid,'start_line':2,'end_line':2}).json()
    assert valid_line['buffer_sha256']==hashlib.sha256(bad).hexdigest()
    assert valid_line['content'].encode()==bad.splitlines(keepends=True)[1]
    assert content(ws,old,source).encode()==raw
    # D-layer corruption of this owned test DB must fail instead of serving a
    # new buffer under an old stored identity. Other immutable indexes survive.
    with sqlite3.connect(ws.database) as c:
        c.execute('UPDATE files SET content=? WHERE index_id=? AND path=?',(b'// corrupted\n',invalid,str(source)))
    corrupt=client.get('/api/source',params={**params,'index_id':invalid,'start_line':1,'end_line':1})
    assert corrupt.status_code==503 and corrupt.json()['error']['code']=='source_integrity_error'
    assert content(ws,old,source).encode()==raw
