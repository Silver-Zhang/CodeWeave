"""Compare immutable facts, separating semantic relations from source positions."""
from collections import Counter, defaultdict
import difflib
import json
import sqlite3
from pathlib import Path

from codeweave.index import IndexError, _connection, _json, _sha256, query
from codeweave.schema import validate
from codeweave.workspace import bounded

MAX_SYMBOLS = 50_000
MAX_CALLS = 100_000


def metadata(c, index_id: str) -> dict:
    row = c.execute('SELECT * FROM index_metadata WHERE index_id=?', (index_id,)).fetchone()
    if not row:
        raise IndexError('index_not_found', 'comparison index does not exist')
    if not row['snapshot_id'] or row['source_kind'] == 'legacy_observed_inputs':
        raise IndexError('not_comparable_legacy_index', 'rebuild both sides with protocol3; legacy facts remain readable')
    result = dict(row)
    result['provenance'] = json.loads(result['provenance'])
    return result


def _side(c, index_id: str) -> dict:
    meta = metadata(c, index_id)
    count = c.execute('SELECT count(*) FROM symbols WHERE index_id=?', (index_id,)).fetchone()[0]
    calls = c.execute('SELECT count(*),coalesce(sum(length(anchors)),0) FROM calls WHERE index_id=?', (index_id,)).fetchone()
    if count > MAX_SYMBOLS or calls[0] > MAX_CALLS or calls[1] > 33_554_432:
        raise IndexError('diff_limit', 'comparison exceeds beta limits: 50000 symbols / 100000 sites / 32 MiB call evidence')
    symbols = {}
    for r in c.execute('SELECT * FROM symbols WHERE index_id=?', (index_id,)):
        row = dict(r); row['anchors'] = json.loads(row['anchors']); row['units'] = []
        symbols[row['usr']] = row
    for usr, tu in c.execute('SELECT usr,source_file FROM symbol_units WHERE index_id=?', (index_id,)):
        symbols[usr]['units'].append(tu)
    reads = defaultdict(dict)
    for tu, path, sha in c.execute(
            'SELECT u.source_file,u.path,f.sha256 FROM unit_files u JOIN files f '
            'ON f.index_id=u.index_id AND f.path=u.path WHERE u.index_id=?', (index_id,)):
        reads[tu][path] = sha
    evidence = {r[0]: json.loads(r[1]) for r in c.execute('SELECT usr,evidence FROM symbol_evidence WHERE index_id=?', (index_id,))}
    for usr,row in symbols.items():row['evidence']=evidence.get(usr,{})
    call_evidence = {r[0]: json.loads(r[1]) for r in c.execute('SELECT instance_id,evidence FROM call_evidence WHERE index_id=?', (index_id,))}
    rows = []
    for r in c.execute('SELECT * FROM calls WHERE index_id=? ORDER BY file,line,column_number,instance_id', (index_id,)):
        row = dict(r); row['syntax'] = call_evidence.get(row['instance_id']); row.pop('anchors'); row.pop('id')
        rows.append(row)
    manifest = json.loads(c.execute('SELECT manifest FROM snapshots WHERE id=?', (meta['snapshot_id'],)).fetchone()[0])
    project = manifest['roots'][0]
    files = {str(Path(project['root'])/r['path']): r for r in project['entries'] if r['kind'] == 'file'}
    return {'meta': meta, 'symbols': symbols, 'evidence': evidence, 'calls': rows, 'files': files,
            'reads': reads, 'context_cache': {},
            'units': {r[0] for r in c.execute('SELECT source_file FROM units WHERE index_id=?', (index_id,))}}


def _symbol_inputs(side: dict, symbol: dict) -> dict:
    """TU observations are conservative input context, not runtime causality.

    Definition/primary source buffers have their own range and location deltas.
    Excluding them keeps a moved function or another body edit from becoming a
    context change for every unchanged function in that source file.
    """
    key = (tuple(sorted(symbol['units'])), symbol['definition_file'])
    cache = side['context_cache']
    if key not in cache:
        inputs = {}
        for tu in key[0]:
            for path, sha in side['reads'].get(tu, {}).items():
                if path not in {tu, key[1]}:
                    inputs[path] = sha
        cache[key] = inputs
    return cache[key]


def _context_changes(a: dict, b: dict, old: dict, new: dict) -> list[dict]:
    before, after = _symbol_inputs(a, old), _symbol_inputs(b, new)
    changes = []
    if a['meta']['profile_id'] != b['meta']['profile_id']:
        changes.append({'kind': 'profile', 'before': a['meta']['profile_id'],
                        'after': b['meta']['profile_id']})
    for path in sorted(set(before) | set(after)):
        if before.get(path) != after.get(path):
            changes.append({'kind': 'input', 'path': path,
                            'before_sha256': before.get(path), 'after_sha256': after.get(path)})
    # An AST hint may also reflect a macro in the definition's own buffer.
    if old['anchors'].get('definition_odr') != new['anchors'].get('definition_odr'):
        before_anchor = old['anchors'].get('definition') or {}
        after_anchor = new['anchors'].get('definition') or {}
        if (before_anchor.get('file') == after_anchor.get('file') and
                before_anchor.get('sha256') != after_anchor.get('sha256')):
            changes.append({'kind': 'definition_buffer', 'path': before_anchor['file'],
                            'before_sha256': before_anchor['sha256'], 'after_sha256': after_anchor['sha256']})
    return changes


def _context_evidence(changes: list[dict], old: dict, new: dict) -> dict:
    preview = []
    for item in changes[:8]:
        if len(_json(preview + [item]).encode('utf-8')) > 8192:
            break
        preview.append(item)
    return {'changed_inputs': preview, 'changed_input_count': len(changes),
            'changed_inputs_truncated': len(preview) != len(changes),
            'changed_inputs_sha256': _sha256(_json(changes).encode('utf-8')),
            'changed_inputs_hash_format': 'json-sort-keys-compact-ascii-v1',
            'odr_hint': {'before': old['anchors'].get('definition_odr'),
                         'after': new['anchors'].get('definition_odr'), 'authoritative': False},
            'context_scope': 'positive reads of observer TUs; excludes definition and primary source buffers',
            'confidence': 'possible input-context change, not runtime behavior'}


def symbol_summary(row: dict | None) -> dict | None:
    if row is None:
        return None
    anchor = row['anchors']['definition'] or row['anchors']['declaration']
    return {k: row[k] for k in ('index_id', 'usr', 'name', 'kind')} | {
        'file': anchor['file'], 'line': anchor['line'], 'column': anchor['column'], 'sha256': anchor['sha256'],
        'units': sorted(row['units'])[:20], 'unit_count': len(row['units']),
        'definition_range':row['evidence'].get('definition_range')}


def call_summary(row: dict | None) -> dict | None:
    if row is None:
        return None
    return {k: row[k] for k in ('index_id','instance_id','caller_usr','target_usr','dispatch','file','line','column_number')} | {'column': row['column_number'],'syntax':row['syntax']}


def removed_scope(old: dict, new: dict, usr: str) -> bool:
    """A source still exists but its command disappeared: coverage, not deletion."""
    return any(tu not in new['units'] and tu in new['files'] for tu in old['symbols'][usr]['units'])


def compare(database: Path, left: str, right: str, kind: str = 'summary', limit: int = 20,
            offset: int = 0, change: str | None = None) -> dict:
    if not 1 <= limit <= 200 or not 0 <= offset < 2**63 or kind not in {'summary','symbols','calls','files'}:
        raise IndexError('invalid_arguments', 'invalid comparison kind or pagination')
    c = _connection(database, True)
    try:
        validate(c)
        if c.execute('PRAGMA user_version').fetchone()[0] != 4:
            raise IndexError('not_comparable_legacy_index', 'protocol3 indexes are required')
        c.execute('BEGIN')
        a, b = _side(c, left), _side(c, right)
        if a['meta']['workspace_id'] != b['meta']['workspace_id'] or a['meta']['policy'] != b['meta']['policy']:
            raise IndexError('indexes_not_comparable', 'indexes must share workspace and semantic policy')
        # A different analyzer/Clang/runtime policy is not a source-only change.
        tools = [json.loads(c.execute('SELECT toolchain FROM indexes WHERE id=?', (i,)).fetchone()[0]) for i in (left,right)]
        if tools[0].get('runtime') != tools[1].get('runtime'):
            raise IndexError('indexes_not_comparable', 'analyzer/runtime versions differ; rebuild a comparable baseline')
        groups: dict[str,list] = {'symbols': [], 'calls': [], 'files': []}

        def event(group: str, event_kind: str, key: object, before=None, after=None, **extra):
            item = {'kind': event_kind, 'before': before, 'after': after, **extra}
            item['event_id'] = _sha256(_json([left,right,event_kind,key]).encode())
            groups[group].append(item)

        for usr in sorted(set(a['symbols'])|set(b['symbols'])):
            old, new = a['symbols'].get(usr), b['symbols'].get(usr)
            before, after = symbol_summary(old), symbol_summary(new)
            if old is None:
                event('symbols','symbol_added',usr,after=after)
                continue
            if new is None:
                event('symbols','out_of_scope' if removed_scope(a,b,usr) else 'symbol_removed',usr,before=before)
                continue
            x, y = a['evidence'].get(usr,{}), b['evidence'].get(usr,{})
            old_range, new_range = x.get('definition_range'), y.get('definition_range')
            context_changes = _context_changes(a, b, old, new)
            if old_range and new_range and old_range['sha256'] != new_range['sha256']:
                event('symbols','definition_text_changed',usr,before,after)
            elif bool(old['definition_file']) != bool(new['definition_file']):
                event('symbols','definition_added' if new['definition_file'] else 'definition_removed',usr,before,after,
                      coverage_change='out_of_scope' if not new['definition_file'] and removed_scope(a,b,usr) else None)
            elif old['definition_file'] and (not old_range or not new_range):
                event('symbols','definition_comparison_unavailable',usr,before,after,confidence='unavailable')
            elif old['definition_file'] and (context_changes or old['anchors'].get('definition_odr') != new['anchors'].get('definition_odr')):
                event('symbols','definition_context_changed',usr,before,after,
                      **_context_evidence(context_changes, old, new))
            if tuple(before[k] for k in ('file','line','column')) != tuple(after[k] for k in ('file','line','column')):
                event('symbols','location_changed',usr,before,after)

        relations = []
        by_caller = []
        for side in (a,b):
            relation = Counter(); callers = defaultdict(list)
            for call in side['calls']:
                relation[(call['caller_usr'],call['target_usr'] or '',call['dispatch'])] += 1
                callers[call['caller_usr']].append(call)
            relations.append(relation);by_caller.append(callers)
        for key in sorted(set(relations[0])|set(relations[1])):
            old_count,new_count = relations[0][key],relations[1][key]
            if old_count == new_count:
                continue
            caller,target,dispatch = key
            old_sites = [call_summary(r) for r in by_caller[0].get(caller,[]) if (r['target_usr'] or '',r['dispatch']) == (target,dispatch)]
            new_sites = [call_summary(r) for r in by_caller[1].get(caller,[]) if (r['target_usr'] or '',r['dispatch']) == (target,dispatch)]
            if not new_count and caller in a['symbols'] and removed_scope(a,b,caller):
                event_kind='relation_out_of_scope'
            else:
                event_kind='relation_added' if not old_count else 'relation_removed' if not new_count else 'relation_count_changed'
            event('calls',event_kind,key,old_sites[0] if old_sites else None,new_sites[0] if new_sites else None,
                  caller_usr=caller,target_usr=target or None,dispatch=dispatch,before_count=old_count,after_count=new_count,
                  before_sites=old_sites[:10],after_sites=new_sites[:10],sites_limited=old_count>10 or new_count>10)
        for caller in sorted(set(by_caller[0])|set(by_caller[1])):
            old_calls,new_calls=by_caller[0].get(caller,[]),by_caller[1].get(caller,[])
            def positions(calls):
                return sorted((r['target_usr'] or '',r['dispatch'],r['file'],r['line'],r['column_number']) for r in calls)
            if positions(old_calls)==positions(new_calls):
                continue
            old_def=a['evidence'].get(caller,{}).get('normalized_definition')
            new_def=b['evidence'].get(caller,{}).get('normalized_definition')
            can_match=bool(old_def and new_def and old_def['token_sha256']==new_def['token_sha256'])
            old_groups,new_groups=defaultdict(list),defaultdict(list)
            for call in old_calls:
                if call['syntax']:old_groups[call['syntax']['token_sha256']].append(call)
            for call in new_calls:
                if call['syntax']:new_groups[call['syntax']['token_sha256']].append(call)
            matched_old=set();matched_new=set()
            if can_match:
                for key in sorted(set(old_groups)&set(new_groups)):
                    if len(old_groups[key])!=1 or len(new_groups[key])!=1:
                        continue
                    old,new=old_groups[key][0],new_groups[key][0]
                    matched_old.add(old['instance_id']);matched_new.add(new['instance_id'])
                    event_kind=None
                    if old['target_usr']!=new['target_usr']:
                        event_kind='target_changed' if old['target_usr'] and new['target_usr'] else 'resolution_changed'
                    elif old['dispatch']!=new['dispatch']:event_kind='dispatch_changed'
                    elif (old['file'],old['line'],old['column_number'])!=(new['file'],new['line'],new['column_number']):event_kind='site_location_changed'
                    if event_kind:event('calls',event_kind,[caller,key],call_summary(old),call_summary(new),match_method='unique-normalized-AST-token-context-v1')
            remaining_old=[call_summary(r) for r in old_calls if r['instance_id'] not in matched_old]
            remaining_new=[call_summary(r) for r in new_calls if r['instance_id'] not in matched_new]
            if remaining_old or remaining_new:
                event('calls','ambiguous_correspondence' if can_match else 'unmatched_sites',caller,
                      remaining_old[0] if remaining_old else None,remaining_new[0] if remaining_new else None,
                      before_sites=remaining_old[:10],after_sites=remaining_new[:10],before_count=len(remaining_old),after_count=len(remaining_new),
                      confidence='unpaired evidence; relation counts are independent')
        for path in sorted(set(a['files'])|set(b['files'])):
            old,new=a['files'].get(path),b['files'].get(path)
            if old and new and old['sha256']==new['sha256']:
                continue
            event('files','file_added' if old is None else 'file_removed' if new is None else 'file_text_changed',path,
                  {'index_id':left,'file':path,'line':1,'column':1,'sha256':old['sha256']} if old else None,
                  {'index_id':right,'file':path,'line':1,'column':1,'sha256':new['sha256']} if new else None)
        comparison_id=_sha256(_json([left,right,POLICY_VERSION]).encode())
        envelope={'comparison_id':comparison_id,'left_index_id':left,'right_index_id':right,
                  'scope':'explicit selected commands; missing coverage is not deletion',
                  'context_scope':'positive reads of observer TUs; definition and primary source buffers excluded',
                  'context_limitations':['TU context is conservative, not a per-definition dependency or runtime proof',
                                         'Same-buffer external constants may appear only in file/source changes; no event is not behavioral equivalence'],
                  'configuration_changed':a['meta']['profile_id']!=b['meta']['profile_id'],
                  'left':a['meta'],'right':b['meta']}
        # Large provenance lists remain available in bounded index status details.
        for side in ('left','right'):
            envelope[side] = {k:envelope[side][k] for k in ('index_id','snapshot_id','profile_id','source_kind')}
            envelope[side]['provenance']={k:a['meta']['provenance'].get(k) if side=='left' else b['meta']['provenance'].get(k) for k in ('base_git_head','represented_git_commit','profile_origin')}
        if kind=='summary':
            envelope['counts']={group:dict(Counter(e['kind'] for e in events)) for group,events in groups.items()}
            return bounded(envelope,32_768)
        events=sorted((e for e in groups[kind] if change is None or e['kind']==change),key=lambda e:(e['kind'],e['event_id']))
        items=events[offset:offset+limit]
        result={**envelope,'items':items,'total_count':len(events),'offset':offset,'limit':limit}
        while True:
            remaining=max(0,len(events)-offset-len(items))
            result.update(returned_count=len(items),remaining_count=remaining,has_more=bool(remaining),next_offset=offset+len(items) if remaining else None)
            try:return bounded(result)
            except IndexError:
                if len(items)<=1:raise
                items.pop();result['budget_limited']=True
    except (sqlite3.Error,OSError,ValueError,TypeError,KeyError) as error:
        raise IndexError('database_error','comparison database query failed') from error
    finally:
        c.close()


POLICY_VERSION='m2a-diff-v2-readsets'


def source_diff(database: Path, left: str, right: str, left_path: str | None, right_path: str | None,
                left_start: int = 1, right_start: int = 1, lines: int = 40) -> dict:
    if not left_path and not right_path or not 1<=lines<=200 or left_start<1 or right_start<1:
        raise IndexError('invalid_range','source diff needs registered path(s), positive starts and 1..200 lines')
    # Also validates pair identity/legacy/profile compatibility.
    pair=compare(database,left,right)
    sides=[]
    for index_id,path,start in ((left,left_path,left_start),(right,right_path,right_start)):
        sides.append(query(database,'source',path,index_id,start_line=start,end_line=start+lines-1,clamp_end=True) if path else None)
    old,new=sides
    text=''.join(difflib.unified_diff((old['content'] if old else '').splitlines(True),(new['content'] if new else '').splitlines(True),fromfile=left+':'+(left_path or '(absent)'),tofile=right+':'+(right_path or '(absent)')))
    return bounded({'comparison_id':pair['comparison_id'],'left_index_id':left,'right_index_id':right,'left':old,'right':new,'unified_diff':text,'windowed':True})
