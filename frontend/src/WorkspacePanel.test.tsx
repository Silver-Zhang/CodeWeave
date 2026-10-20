import {act,cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react';
import {afterEach,expect,it,vi} from 'vitest';
import WorkspacePanel from './WorkspacePanel';
import {api,type ChangePage} from './api';
vi.mock('./api',()=>({api:vi.fn()}));
vi.mock('./DiffPanel',()=>({default:()=> <p>diff view</p>}));
afterEach(()=>{cleanup();vi.restoreAllMocks();});
const indexes=['a','b','c'].map(index_id=>({index_id,created_at:index_id,source_kind:'workspace'}));
const props={indexes,onPublished:vi.fn(),onHighlight:vi.fn(),onInspect:vi.fn(),onConfigured:vi.fn()};
const workspace={configured:true,refresh_enabled:true,generation:1,project_root:'/owned',latest_successful_index:'a',last_attempt:null};
const summary=(left:string,right:string)=>({left_index_id:left,right_index_id:right,counts:{symbols:{},calls:{},files:{}},left:{},right:{},scope:'selected commands'});
function page(left:string,right:string,label:string):ChangePage{return {left_index_id:left,right_index_id:right,comparison_id:'pair',items:[{event_id:label,kind:label,before:null,after:{index_id:right,file:'/owned/a.cpp',line:1,column:1}}],returned_count:1,total_count:1,next_offset:null,has_more:false};}
function setup(override:(route:string,params:Record<string,string|number>)=>Promise<unknown>|undefined=()=>undefined){
 vi.mocked(api).mockImplementation(async(route,params={})=>{
  const value=override(route,params);if(value)return await value as never;
  if(route==='workspace')return workspace as never;
  if(route==='session')return {csrf_token:'token'} as never;
  if(route==='diff')return summary(String(params.left),String(params.right)) as never;
  if(route.startsWith('diff/'))return page(String(params.left),String(params.right),route) as never;
  throw new Error(route);
 });
 render(<WorkspacePanel {...props}/>);
}
function deferred<T>(){let resolve!:(v:T)=>void;const promise=new Promise<T>(r=>{resolve=r;});return {resolve,promise};}
it('keeps late aborted category results out of the active category',async()=>{
 const late=deferred<ChangePage>();setup(route=>route==='diff/symbols'?late.promise:undefined);
 await screen.findByLabelText('Change group');
 fireEvent.change(screen.getByLabelText('Change group'),{target:{value:'calls'}});
 await screen.findByText('diff/calls');
 await act(async()=>late.resolve(page('b','a','stale-symbol')));
 expect(screen.queryByText('stale-symbol')).toBeNull();expect(screen.getByText('diff/calls')).toBeTruthy();
});
it('clears old pair source and ignores delayed comparison response',async()=>{
 const late=deferred<ChangePage>();setup((route,p)=>route==='diff/symbols'&&p.left==='b'?late.promise:undefined);
 await screen.findByLabelText('Left comparison index');
 fireEvent.change(screen.getByLabelText('Left comparison index'),{target:{value:'c'}});
 await screen.findByText('diff/symbols');
 await act(async()=>late.resolve(page('b','a','stale-pair')));
 expect(screen.queryByText('stale-pair')).toBeNull();expect((screen.getByLabelText('Left comparison index') as HTMLSelectElement).value).toBe('c');
});
it('POST uses trusted bootstrap token and only generation/token/source kind',async()=>{
 const fetcher=vi.spyOn(globalThis,'fetch').mockResolvedValue({ok:true,json:async()=>({request_id:'real-request',state:'queued',unit_count:0})} as Response);
 setup();await waitFor(()=>expect((screen.getByRole('button',{name:'刷新工作区'}) as HTMLButtonElement).disabled).toBe(false));
 fireEvent.click(screen.getByRole('button',{name:'刷新工作区'}));
 await waitFor(()=>expect(fetcher).toHaveBeenCalledTimes(1));
 const [url,init]=fetcher.mock.calls[0];expect(url).toBe('/api/workspace/refresh');
 expect(init?.headers).toEqual({'Content-Type':'application/json','X-CodeWeave-Token':'token'});
 const body=JSON.parse(String(init?.body));expect(Object.keys(body).sort()).toEqual(['generation','idempotency_token','source_kind']);expect(body.generation).toBe(1);
 await screen.findByText('real-request');
});
