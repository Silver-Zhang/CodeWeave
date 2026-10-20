import { lazy, Suspense, useEffect, useRef, useState } from 'react';
import { api, type Attempt, type ChangeEvent, type ChangePage, type Comparison, type IndexEntry, type SourceDiff, type WorkspaceStatus } from './api';
import SourceBoundary from './SourceBoundary';
const DiffPanel = lazy(() => import('./DiffPanel'));
const text = (v: unknown) => typeof v === 'string' ? v : JSON.stringify(v);
const terminal = new Set(['published', 'failed', 'cancelled', 'interrupted']);
interface Props { indexes: IndexEntry[]; onPublished: () => void; onHighlight: (event: ChangeEvent | null) => void; onInspect: (index: string, usr: string) => void; onConfigured: (configured: boolean) => void }
export default function WorkspacePanel(props: Props) {
  const callbacks = useRef(props); callbacks.current = props;
  const [workspace, setWorkspace] = useState<WorkspaceStatus>();
  const [error, setError] = useState(''); const [posting, setPosting] = useState(false);
  const [sessionReady, setSessionReady] = useState(false);
  const csrf = useRef<string | null>(null); const seen = useRef<string | null>(null);
  const initialized = useRef(false);
  const refreshBase = useRef<string | null>(null);
  const [available, setAvailable] = useState<{left:string;right:string}>();
  const [left, setLeft] = useState(''); const [right, setRight] = useState('');
  const [group, setGroup] = useState('symbols'); const [filter, setFilter] = useState(''); const [offset, setOffset] = useState(0);
  const [summaryData, setSummary] = useState<Comparison>(); const [pageData, setPage] = useState<ChangePage>();
  const [sourceData, setSource] = useState<SourceDiff>(); const [selected, setSelected] = useState<{left:string;right:string;event:ChangeEvent}>();
  const sourceController = useRef<AbortController | undefined>(undefined); const actionController = useRef<AbortController | undefined>(undefined);
  const summary = summaryData?.left_index_id === left && summaryData.right_index_id === right ? summaryData : undefined;
  const page = pageData?.left_index_id === left && pageData.right_index_id === right ? pageData : undefined;
  const source = sourceData?.left_index_id === left && sourceData.right_index_id === right ? sourceData : undefined;
  const selection = selected?.left === left && selected.right === right ? selected.event : undefined;
  useEffect(() => {
    const controller = new AbortController(); let timer: ReturnType<typeof setTimeout> | undefined; let shouldPoll = true;
    async function tick() {
      try {
        const data = await api<WorkspaceStatus>('workspace', {}, controller.signal);
        if (controller.signal.aborted) return;
        setWorkspace(data); callbacks.current.onConfigured(data.configured);
        if (!data.configured) {shouldPoll=false;return;}
        if (data.refresh_enabled && !csrf.current) {
          const session = await api<{csrf_token:string}>('session', {}, controller.signal);
          csrf.current = session.csrf_token;
          setSessionReady(true);
        }
        const job = data.last_attempt;
        if (job?.state === 'published' && job.result_index_id && seen.current !== job.result_index_id) {
          if (initialized.current) {
            callbacks.current.onPublished();
            const prior = job.source_kind === 'head' ? job.result_index_id : refreshBase.current;
            const latest = job.source_kind === 'head' ? data.latest_successful_index : job.result_index_id;
            if (prior && latest) setAvailable({left:prior,right:latest});
          }
          seen.current = job.result_index_id;
        }
        initialized.current=true;
      } catch (e) { if (!controller.signal.aborted) setError(String(e)); }
      finally { if (!controller.signal.aborted && shouldPoll) timer = setTimeout(tick, 800); }
    }
    void tick();
    return () => { controller.abort(); clearTimeout(timer); sourceController.current?.abort(); actionController.current?.abort(); };
  }, []);
  useEffect(() => {
    if (!workspace?.configured || left || right) return;
    const recent = props.indexes.filter(i => i.source_kind === 'workspace');
    if (recent.length >= 2) { setLeft(recent[1].index_id); setRight(recent[0].index_id); }
  }, [workspace?.configured, props.indexes, left, right]);
  useEffect(() => {
    const controller = new AbortController(); sourceController.current?.abort(); setSource(undefined); setSelected(undefined);
    callbacks.current.onHighlight(null);
    if (!left || !right) return;
    api<Comparison>('diff', {left,right}, controller.signal).then(value => {if (!controller.signal.aborted) setSummary(value);}).catch(e => {if (!controller.signal.aborted) setError(String(e));});
    return () => controller.abort();
  }, [left,right]);
  useEffect(() => {
    const controller = new AbortController(); setPage(undefined);
    if (!left || !right) return;
    const params: Record<string,string|number> = {left,right,limit:12,offset}; if (filter) params.change=filter;
    api<ChangePage>(`diff/${group}`,params,controller.signal).then(value => {if (!controller.signal.aborted) setPage(value);}).catch(e => {if (!controller.signal.aborted) setError(String(e));});
    return () => controller.abort();
  }, [left,right,group,filter,offset]);
  async function post(route: string, body: unknown) {
    actionController.current?.abort(); const controller = new AbortController(); actionController.current = controller;
    setPosting(true); setError('');
    try {
      const response = await fetch(`/api/${route}`, {method:'POST', headers:{'Content-Type':'application/json','X-CodeWeave-Token':csrf.current ?? ''},body:JSON.stringify(body),signal:controller.signal});
      const data = await response.json();
      if (!response.ok) throw new Error(`${data.error?.code}: ${data.error?.message}`);
      if (!controller.signal.aborted) setWorkspace(old => old ? {...old,last_attempt:data as Attempt} : old);
    } catch (e) {if (!controller.signal.aborted) setError(String(e));}
    finally {if (!controller.signal.aborted) setPosting(false);}
  }
  function refresh(kind: string) {
    if (!workspace) return;
    refreshBase.current=workspace.latest_successful_index;
    void post('workspace/refresh',{generation:workspace.generation,idempotency_token:crypto.randomUUID(),source_kind:kind});
  }
  async function inspect(event: ChangeEvent) {
    sourceController.current?.abort(); const controller = new AbortController(); sourceController.current=controller;
    setSource(undefined); setSelected({left,right,event}); callbacks.current.onHighlight(event); setError('');
    const params: Record<string,string|number> = {left,right,lines:40};
    if (event.before) {params.left_path=event.before.file;params.left_start=Math.max(1,event.before.line-8);}
    if (event.after) {params.right_path=event.after.file;params.right_start=Math.max(1,event.after.line-8);}
    try { const value=await api<SourceDiff>('source-diff',params,controller.signal); if (!controller.signal.aborted) setSource(value); }
    catch(e) {if (!controller.signal.aborted) setError(String(e));}
  }
  if (!workspace?.configured) return null;
  const job=workspace.last_attempt; const busy=posting || workspace.refresh_enabled && !sessionReady || !!job && !terminal.has(job.state);
  const kinds = summary?.counts[group] ?? {};
  return <section className="workspace-panel" id="workspace-panel" aria-label="Workspace refresh and comparison">
    <h2>工作区刷新与版本变化</h2>
    <p className="root-path">{text(workspace.project_root)} · 全量分析保存后的磁盘源码</p>
    <div className="workspace-actions"><button disabled={!workspace.refresh_enabled || busy} onClick={() => refresh('workspace')}>刷新工作区</button>
      <button disabled={!workspace.refresh_enabled || busy} onClick={() => refresh('head')}>建立 HEAD 基线</button>
      {job && !terminal.has(job.state) && <button disabled={posting} onClick={() => void post(`workspace/attempts/${job.request_id}/cancel`,{})}>取消刷新</button>}
      {!workspace.refresh_enabled && <span>只读服务，刷新由本地启动配置禁用。</span>}
    </div>
    {job && <div className={`attempt ${job.state}`} role="status"><code>{job.request_id}</code><strong>{job.state}</strong><span>{job.unit_count} 个已记录TU</span><p>{job.error_code ? job.error_code+' · ' : ''}{text(job.message)}</p></div>}
    {available && <button disabled={busy} onClick={() => {setLeft(available.left);setRight(available.right);setOffset(0);}}>比较上次成功与本次结果</button>}
    {error && <div className="error" role="alert">{error}<button onClick={() => setError('')}>关闭</button></div>}
    <div className="version-pair"><label>左版本<select aria-label="Left comparison index" value={left} onChange={e => {setLeft(e.target.value);setOffset(0);}}><option value="">选择旧版本 / HEAD</option>{props.indexes.map(i => <option key={i.index_id} value={i.index_id}>{i.source_kind ?? 'legacy'} · {i.created_at} · {i.index_id}</option>)}</select></label>
      <label>右版本<select aria-label="Right comparison index" value={right} onChange={e => {setRight(e.target.value);setOffset(0);}}><option value="">选择工作区版本</option>{props.indexes.map(i => <option key={i.index_id} value={i.index_id}>{i.source_kind ?? 'legacy'} · {i.created_at} · {i.index_id}</option>)}</select></label></div>
    {!left || !right ? <p>保存修改后刷新，比较两次成功结果；或建立真实 Git HEAD 基线。历史输入缺失会明确报错，不会代用其他索引。</p> : null}
    {summary && <><p className="scope">{summary.scope} {summary.configuration_changed && '编译配置/工具链输入有变化，差异不能仅归因于源码。'}</p><details><summary>两侧来源、配置与Git身份</summary><pre>{JSON.stringify({left:summary.left,right:summary.right,context_scope:summary.context_scope,context_limitations:summary.context_limitations},null,2)}</pre></details>
      <div className="change-filters"><label>变化类别<select aria-label="Change group" value={group} onChange={e => {setGroup(e.target.value);setFilter('');setOffset(0);}}><option value="symbols">函数变化</option><option value="calls">调用变化</option><option value="files">文件变化</option></select></label>
        <label>筛选<select aria-label="Change filter" value={filter} onChange={e => {setFilter(e.target.value);setOffset(0);}}><option value="">全部变化</option>{Object.entries(kinds).map(([name,count]) => <option key={name} value={name}>{name} ({count})</option>)}</select></label></div>
      <div className="change-list">{page?.items.map(event => <button key={event.event_id} className={selection?.event_id===event.event_id?'active':''} onClick={() => void inspect(event)}><strong>{event.kind}</strong> {event.after?.name ?? event.before?.name ?? event.caller_usr ?? event.after?.file ?? event.before?.file}
        {event.before_count !== undefined && <small> 调用实例 {event.before_count} → {event.after_count}</small>}{event.confidence && <small>{event.confidence}</small>}</button>)}{page && !page.items.length && <p>此类别无变化。</p>}</div>
      <div className="pager"><button disabled={!offset} onClick={() => setOffset(Math.max(0,offset-12))}>上一页变化</button><span>{page?.returned_count ?? 0} / {page?.total_count ?? 0}</span><button disabled={!page?.has_more} onClick={() => setOffset(page?.next_offset ?? 0)}>下一页变化</button></div></>}
    {selection && <div className="change-evidence"><p>{selection.kind} · {selection.match_method ?? selection.confidence ?? '索引静态事实'}</p>{(['before','after'] as const).map(side => {const ref=selection[side]; const usr=ref?.usr ?? ref?.caller_usr;return ref && usr ? <button key={side} onClick={() => callbacks.current.onInspect(ref.index_id,usr)}>{side==='before'?'在左版地图查看':'在右版地图查看'}</button> : null;})}
      {selection.changed_inputs && <details><summary>输入上下文证据（{selection.changed_input_count} 项{selection.changed_inputs_truncated ? '，有界预览' : ''}）</summary><pre>{JSON.stringify({scope:selection.context_scope,changed_inputs:selection.changed_inputs,changed_inputs_sha256:selection.changed_inputs_sha256,changed_inputs_hash_format:selection.changed_inputs_hash_format,odr_hint:selection.odr_hint},null,2)}</pre></details>}</div>}
    {source && <div className="source-diff"><div className="source-pair-label"><code>左 {source.left?.index_id} · {source.left?.path ?? '实体不存在'}:{source.left?.start_line}</code><code>右 {source.right?.index_id} · {source.right?.path ?? '实体不存在'}:{source.right?.start_line}</code></div>
      <p>索引保存源码的有界窗口 Diff；每侧最多200行 / 32768字节。</p>{(source.left?.range_sha256 || source.right?.range_sha256) && <details><summary>两侧源码哈希与字节范围</summary><pre>{JSON.stringify({left:source.left && {buffer_sha256:source.left.buffer_sha256,range_sha256:source.left.range_sha256,begin:source.left.begin,end:source.left.end},right:source.right && {buffer_sha256:source.right.buffer_sha256,range_sha256:source.right.range_sha256,begin:source.right.begin,end:source.right.end}},null,2)}</pre></details>}<SourceBoundary key={source.comparison_id+selection?.event_id} content={source.unified_diff}><Suspense fallback={<p>加载源码Diff编辑器…</p>}><DiffPanel source={source}/></Suspense></SourceBoundary><details><summary>Unified source diff</summary><pre>{source.unified_diff || '此窗口源码文本相同，检查位置/调用证据。'}</pre></details></div>}
  </section>;
}
