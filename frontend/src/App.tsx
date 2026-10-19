import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Background, Controls, MarkerType, ReactFlow } from '@xyflow/react';
import { api, type Entity, type Graph, type Page, type Source, type Status } from './api';
import SourceBoundary from './SourceBoundary';
import { BUDGET, flatten, mergeGraph, position, type MapState } from './graph';
const SourcePanel = lazy(() => import('./SourcePanel'));
const describe = (value: unknown) => typeof value === 'string' ? value : JSON.stringify(value);
const message = (error: unknown) => error instanceof Error ? error.message : String(error);
export default function App() {
  const [indexes, setIndexes] = useState<{ index_id: string; created_at: string }[]>([]);
  const [indexPage, setIndexPage] = useState<number | null>(0);
  const [indexId, setIndexId] = useState('');
  const [statusData, setStatus] = useState<Status>();
  const status = statusData?.index_id === indexId ? statusData : undefined;
  const [term, setTerm] = useState(''); const [offset, setOffset] = useState(0);
  const [resultsData, setResults] = useState<Page<Entity>>();
  const results = resultsData?.index_id === indexId ? resultsData : undefined; const [searching, setSearching] = useState(false);
  const [mapData, setMap] = useState<MapState>();
  const map = mapData?.index_id === indexId ? mapData : undefined; const [selectedData, setSelected] = useState<Entity>();
  const selected = selectedData?.index_id === indexId ? selectedData : undefined;
  const [requests, setRequests] = useState(0); const requestCount = useRef(0);
  const [sourceData, setSource] = useState<Source>();
  const source = sourceData?.index_id === indexId ? sourceData : undefined; const [location, setLocation] = useState({ line: 1, column: 1 });
  const [evidence, setEvidence] = useState<unknown>(); const [busy, setBusy] = useState(false);
  const [error, setError] = useState(''); const [notice, setNotice] = useState('');
  const epoch = useRef(0); const action = useRef<AbortController | undefined>(undefined);
  const sourceAction = useRef<AbortController | undefined>(undefined);
  const flow = useRef<{ fitView: (options: { padding: number; maxZoom?: number }) => Promise<boolean> } | null>(null);

  const loadIndexes = useCallback(async (at: number) => {
    try { const page = await api<Page<{ index_id: string; created_at: string }>>('indexes', { limit: 20, offset: at });
      setIndexes(old => at ? [...old, ...page.items.filter(item => !old.some(p => p.index_id === item.index_id))] : page.items);
      setIndexPage(page.next_offset); if (!at) setIndexId(page.index_id);
    } catch (e) { setError(message(e)); }
  }, []);
  useEffect(() => { void loadIndexes(0); return () => { action.current?.abort(); sourceAction.current?.abort(); }; }, [loadIndexes]);
  useEffect(() => {
    epoch.current++; action.current?.abort(); sourceAction.current?.abort();
    setMap(undefined); setSelected(undefined); setSource(undefined); setEvidence(undefined); setStatus(undefined); setResults(undefined);
    setRequests(0); setSearching(false); requestCount.current = 0; setBusy(false); setOffset(0); setError(''); setNotice('');
    if (!indexId) return;
    const controller = new AbortController();
    api<Status>('status', { index_id: indexId }, controller.signal).then(setStatus).catch(e => { if (!controller.signal.aborted) setError(message(e)); });
    return () => controller.abort();
  }, [indexId]);
  useEffect(() => {
    if (!indexId) return;
    const controller = new AbortController();
    setSearching(true); setResults(undefined);
    const timer = setTimeout(() => { api<Page<Entity>>('search', { index_id: indexId, q: term, limit: 12, offset }, controller.signal)
      .then(setResults).catch(e => { if (!controller.signal.aborted) setError(message(e)); })
      .finally(() => { if (!controller.signal.aborted) setSearching(false); }); }, 200);
    return () => { clearTimeout(timer); controller.abort(); };
  }, [indexId, term, offset]);

  async function locate(path?: string, line?: number, column = 1, fact?: unknown) {
    sourceAction.current?.abort(); const controller = new AbortController(); sourceAction.current = controller;
    setSource(undefined); setEvidence(fact); if (!path || !line) { setNotice('No archived source location for this target.'); return; }
    const id = indexId; setNotice('Loading indexed source…');
    try { const snippet = await api<Source>('source', { index_id: id, path, start_line: Math.max(1, line - 8), end_line: line + 16, clamp_end: 'true' }, controller.signal);
      if (!controller.signal.aborted) { setSource(snippet); setLocation({ line, column }); setNotice(''); }
    } catch (e) { if (!controller.signal.aborted) { setNotice(''); setError(message(e)); } }
  }
  function select(entity: Entity) {
    setSelected(entity);
    void locate(entity.definition_file ?? entity.declaration_file, entity.definition_line ?? entity.declaration_line,
      entity.definition_column ?? entity.declaration_column ?? 1, entity);
  }
  async function expand(entity: Entity, reset = false) {
    if (entity.kind === 'unknown' || entity.kind === 'external') { select(entity); return; }
    if (!reset && map?.batches[entity.usr]) { setNotice('This entity is already expanded.'); return; }
    if (!reset && requestCount.current >= BUDGET.requests) { setError('Request budget reached. Reset the map to continue.'); return; }
    action.current?.abort(); const controller = new AbortController(); action.current = controller;
    const token = ++epoch.current; setBusy(true); setError(''); setNotice('Loading one-hop neighborhood…');
    if (reset) { requestCount.current = 0; setMap(undefined); setSource(undefined); sourceAction.current?.abort(); }
    requestCount.current++; setRequests(requestCount.current);
    try { const graph = await api<Graph>('neighbors', { index_id: indexId, usr: entity.usr, limit: 12 }, controller.signal);
      if (epoch.current !== token) return;
      setMap(mergeGraph(reset || !map ? { index_id: indexId, center: entity.usr, batches: {} } : map, graph));
      // Expansion adds facts without overriding a newer node/call-site selection.
      if (reset) select(entity);
      setNotice(Object.values(graph.pages).some(p => p.has_more) ? 'Local page capped at 12 calls per direction. Use the paged calls API/CLI for remaining evidence.' : '');
    } catch (e) { if (!controller.signal.aborted) { setNotice(''); setError(message(e)); } }
    finally { if (epoch.current === token) setBusy(false); }
  }
  const visible = useMemo(() => map ? flatten(map) : { nodes: [], edges: [] }, [map]);
  const nodes = useMemo(() => visible.nodes.map((entity, i) => ({ id: entity.usr, position: position(i),
    data: { label: <div className="node-label"><span>{entity.kind === 'unknown' ? '? Unresolved target' : entity.name}</span><small>{entity.kind} {map?.batches[entity.usr] ? '· expanded' : ''}<br/>{(entity.definition_file ?? entity.declaration_file)?.split('/').slice(-2).join('/')}:{entity.definition_line ?? entity.declaration_line}</small></div> },
    selected: selected?.usr === entity.usr, style: { width: 270, borderColor: entity.kind === 'unknown' ? '#d4a95d' : selected?.usr === entity.usr ? '#5cdbc6' : '#3a4a59' }
  })), [visible.nodes, selected, map]);
  const edges = useMemo(() => visible.edges.map(call => ({ id: call.instance_id, source: call.source, target: call.target,
    label: call.dispatch === 'virtual' ? 'virtual · static target' : call.dispatch, markerEnd: { type: MarkerType.ArrowClosed },
    style: { stroke: call.dispatch === 'direct' ? '#6ba6b4' : '#d4a95d', strokeWidth: 2, strokeDasharray: call.dispatch === 'direct' ? undefined : '6 4' },
    labelStyle: { fill: '#e1e8ec', fontSize: 11 }, labelBgStyle: { fill: '#18232e' },
  })), [visible.edges]);
  useEffect(() => { if (nodes.length) requestAnimationFrame(() => { void flow.current?.fitView({ padding: 0.18, maxZoom: 1 }); }); }, [nodes.length]);
  function collapse() {
    if (!map || !selected) return;
    const batches = { ...map.batches };
    if (selected.usr === map.center) { for (const key of Object.keys(batches)) if (key !== map.center) delete batches[key]; }
    else delete batches[selected.usr];
    setMap({ ...map, batches }); setNotice('Collapsed expansion; shared neighbors remain visible.');
  }
  return <div className="app">
    <header><div className="brand"><b>⌘</b><div>CodeWeave<small>STRUCTURE EXPLORER</small></div></div><div className="header-note">LOCAL INDEX / M1<span className="live-dot" /> Read only</div></header>
    <div className="workspace"><aside className="sidebar">
      <section><div className="eyebrow">01 / INDEX</div><label htmlFor="index">Snapshot identity</label>
        <select id="index" value={indexId} onChange={e => setIndexId(e.target.value)}><option value="" disabled>Select an index</option>{indexes.map(i => <option key={i.index_id} value={i.index_id}>{i.created_at} · {i.index_id}</option>)}</select>
        {indexPage !== null && <button className="text-button" onClick={() => void loadIndexes(indexPage)}>Load older indexes</button>}
        <button className="text-button" onClick={() => void loadIndexes(0)}>Refresh indexes</button>
        {status && <><code className="identity">{status.index_id}</code><div className="root-path">{describe(status.project_root)}</div><div className="stats"><span><b>{status.unit_count}</b> units</span><span><b>{status.input_count}</b> inputs</span><span>{describe(status.coverage)}</span></div><details><summary>Coverage & limitations</summary><p>{JSON.stringify(status.diagnostic_summary)}</p>{status.limitations.map(s => <p key={s}>{s}</p>)}</details></>}
      </section>
      <section className="search-section"><div className="eyebrow">02 / FIND SYMBOL</div><label htmlFor="search">Function or method</label><input id="search" placeholder="Search names and scopes…" value={term} onChange={e => { setTerm(e.target.value); setOffset(0); }} disabled={!indexId}/>
        <div role="status" className="search-count">{searching ? 'Searching…' : results ? `${results.total_count} matches · page ${Math.floor(offset / 12) + 1}` : 'Choose a snapshot to begin'}</div>
        <div className="results">{results?.items.map(entity => <button className={`result ${selected?.usr === entity.usr ? 'active' : ''}`} key={entity.usr} onClick={() => void expand(entity, true)}><strong>{entity.name}</strong><small>{entity.kind} · {(entity.definition_file ?? entity.declaration_file)?.split('/').slice(-2).join('/')}:{entity.definition_line ?? entity.declaration_line}</small><code>{entity.usr}</code></button>)}{results && !results.items.length && <p>No matching symbols.</p>}</div>
        <div className="pager"><button disabled={!offset || searching} onClick={() => setOffset(Math.max(0, offset - 12))}>Previous</button><button disabled={!results?.has_more || searching} onClick={() => setOffset(results?.next_offset ?? 0)}>Next</button></div>
      </section><footer>Static structure, never a runtime trace.</footer>
    </aside>
    <main><div className="map-toolbar"><div><span className="eyebrow">03 / CALL MAP</span><span className="counts">{nodes.length}/{BUDGET.nodes} nodes · {edges.length}/{BUDGET.edges} sites · {requests}/{BUDGET.requests} requests</span></div><div className="toolbar-buttons"><button disabled={!selected || busy} onClick={() => selected && void expand(selected)}>Expand selected</button><button disabled={!map || busy} onClick={collapse}>Collapse selected</button><button disabled={!map} onClick={() => void flow.current?.fitView({ padding: 0.2 })}>Center</button><button disabled={!map || busy} onClick={() => { const root = visible.nodes.find(n => n.usr === map?.center); if (root) void expand(root, true); }}>Reset</button></div></div>
      {error && <div role="alert" className="error">{error}<button aria-label="Dismiss error" onClick={() => setError('')}>×</button></div>}
      {notice && <div role="status" className="notice">{notice}</div>}
      <div className="canvas" aria-label="Call map"><ReactFlow nodes={nodes} edges={edges} onInit={instance => { flow.current = instance; }} nodesDraggable={false} nodesConnectable={false} edgesReconnectable={false} deleteKeyCode={null} minZoom={0.15} maxZoom={1.7}
        onNodeClick={(_, node) => { const entity = visible.nodes.find(n => n.usr === node.id); if (entity) select(entity); }}
        onEdgeClick={(_, edge) => { const call = visible.edges.find(c => c.instance_id === edge.id); if (call) void locate(call.file, call.line, call.column_number, call); }}><Background color="#263746" gap={24}/><Controls showInteractive={false}/></ReactFlow>
        {!nodes.length && <div className="empty-map"><span>↗</span><h1>Follow the code.</h1><p>Search a symbol to open its local call map.<br/>Expand only what you need to read.</p></div>}
        <div className="legend"><span>— direct</span><span>┄ virtual / unresolved</span><span>One edge = one indexed call site</span></div>
      </div>
      <section className="source-section"><div className="source-heading"><div className="eyebrow">04 / INDEXED SOURCE</div>{source ? <><code>{source.path}:{location.line}:{location.column}</code><small>{source.index_id} · snapshot</small></> : <span>Select a node or call site to inspect its saved source.</span>}</div>
        {source && <SourceBoundary key={`${source.index_id}:${source.path}:${source.start_line}`} content={source.content}><Suspense fallback={<p>Loading source editor…</p>}><SourcePanel source={source} line={location.line} column={location.column}/></Suspense></SourceBoundary>}
        {selected && <div className="call-sites"><strong>{selected.name}</strong><code>{selected.usr}</code>{visible.edges.filter(c => c.source === selected.usr || c.target === selected.usr).map(c => <button key={c.instance_id} onClick={() => void locate(c.file, c.line, c.column_number, c)}>{c.dispatch} · {c.file.split('/').pop()}:{c.line}:{c.column_number}</button>)}<details><summary>Entity / call-site provenance</summary><pre>{JSON.stringify(evidence ?? selected, null, 2)}</pre></details></div>}
      </section>
    </main></div>
  </div>;
}
