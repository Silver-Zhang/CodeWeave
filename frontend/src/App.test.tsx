import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import App from './App';
import { api, type Entity, type Graph } from './api';
vi.mock('./api', () => ({ api: vi.fn() }));
vi.mock('./SourcePanel', () => ({ default: ({ source }: { source: {content:string} }) => <pre>{source.content}</pre> }));
vi.mock('@xyflow/react', () => ({ MarkerType:{ArrowClosed:'arrow'}, Background:()=>null, Controls:()=>null,
  ReactFlow: ({nodes,onNodeClick,children}: {nodes:{id:string}[];onNodeClick:(event:unknown,node:{id:string})=>void;children:unknown}) => <div>{nodes.map(n=><button key={n.id} onClick={()=>onNodeClick(null,n)}>node {n.id}</button>)}{children as React.ReactNode}</div> }));
afterEach(cleanup);
const a: Entity = { index_id:'i1',usr:'a',name:'alpha',kind:'function',definition_file:'/owned/alpha.cpp',definition_line:3,definition_column:2 };
const b: Entity = { ...a,usr:'b',name:'beta' };
const graph: Graph = {index_id:'i1',center:'a',nodes:[a,b],edges:[],pages:{callees:{has_more:false,total_count:1}}};
function setup(fail = false, pending?: Promise<Graph>) {
  vi.mocked(api).mockImplementation(async (route, params) => {
    if (route === 'indexes') return {index_id:'i1',items:[{index_id:'i1',created_at:'now'},{index_id:'i2',created_at:'old'}],next_offset:null} as never;
    if (route === 'status') return { index_id:params?.index_id,project_root:'/owned',unit_count:2,input_count:3,coverage:'complete',limitations:[],diagnostic_summary:{} } as never;
    if (route === 'search') return {index_id:params?.index_id,items:params?.q === 'absent' ? [] : [a],total_count:params?.q === 'absent' ? 0 : 1,has_more:false} as never;
    if (route === 'neighbors') { if (pending && params?.usr === 'b') return await pending as never; if(fail) throw new Error('database_error: test failure'); return {...graph,center:params?.usr} as never; }
    if (route === 'source') return {index_id:'i1',path:'/owned/alpha.cpp',start_line:1,end_line:4,content:'int alpha() { return beta(); }'} as never;
    throw new Error('unexpected route');
  });
  render(<App/>);
}
it('searches, selects real API entity, expands once, and clears graph when index changes', async () => {
  setup();
  fireEvent.change(await screen.findByLabelText('Function or method'),{target:{value:'alpha'}});
  fireEvent.click(await screen.findByRole('button',{name:/alpha.*function/}));
  await screen.findByRole('button',{name:'node a'});
  await screen.findByText('int alpha() { return beta(); }');
  fireEvent.click(screen.getByRole('button',{name:'node b'}));
  fireEvent.click(screen.getByRole('button',{name:'Expand selected'}));
  await waitFor(()=>expect(vi.mocked(api).mock.calls.filter(([route])=>route==='neighbors')).toHaveLength(2));
  await waitFor(()=>expect((screen.getByRole('button',{name:'Expand selected'}) as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(screen.getByRole('button',{name:'Expand selected'}));
  await screen.findByText('This entity is already expanded.');
  fireEvent.change(screen.getByLabelText('Snapshot identity'),{target:{value:'i2'}});
  await waitFor(()=>expect(screen.queryByRole('button',{name:'node a'})).toBeNull());
});
it('shows errors and permits retry instead of inventing map data', async () => {
  setup(true);fireEvent.click(await screen.findByRole('button',{name:/alpha.*function/}));
  expect((await screen.findByRole('alert')).textContent).toContain('database_error');
  expect(screen.queryByRole('button',{name:'node a'})).toBeNull();
});
it('shows an empty search state', async () => {
  setup();fireEvent.change(screen.getByLabelText('Function or method'),{target:{value:'absent'}});
  await screen.findByText('No matching symbols.');
});

it('keeps the last clicked node when an earlier expansion resolves', async () => {
  let finish!: (value: Graph) => void;
  const pending = new Promise<Graph>(resolve => { finish = resolve; });
  setup(false, pending);
  fireEvent.click(await screen.findByRole('button', { name: /alpha.*function/ }));
  await screen.findByRole('button', { name: 'node b' });
  fireEvent.click(screen.getByRole('button', { name: 'node b' }));
  fireEvent.click(screen.getByRole('button', { name: 'Expand selected' }));
  await waitFor(() => expect((screen.getByRole('button', { name: 'Expand selected' }) as HTMLButtonElement).disabled).toBe(true));
  fireEvent.click(screen.getByRole('button', { name: 'node a' }));
  expect(document.querySelector('.call-sites > strong')?.textContent).toBe('alpha');
  await act(async () => { finish({ ...graph, center: 'b' }); });
  await waitFor(() => expect((screen.getByRole('button', { name: 'Expand selected' }) as HTMLButtonElement).disabled).toBe(false));
  expect(document.querySelector('.call-sites > strong')?.textContent).toBe('alpha');
});
