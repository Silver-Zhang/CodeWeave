import { describe, expect, it } from 'vitest';
import { BUDGET, flatten, mergeGraph } from './graph';
import type { Graph } from './api';
function graph(center: string, names: string[], sites: string[] = []): Graph {
  return { index_id: 'snapshot-a', center, nodes: names.map(usr => ({ index_id: 'snapshot-a', usr, name: usr, kind: 'function' })),
    edges: sites.map(instance_id => ({ index_id: 'snapshot-a', instance_id, caller_usr: 'a', target_usr: 'b', source: 'a', target: 'b', dispatch: 'direct', file: 'a.cpp', line: 1, column_number: 1, anchors: {}, units: [] })), pages: {} };
}
describe('bounded local graph', () => {
  it('deduplicates identities and sites while preserving multiple call instances', () => {
    let state = { index_id: 'snapshot-a', center: 'a', batches: {} };
    state = mergeGraph(state, graph('a', ['a', 'b'], ['first', 'second']));
    state = mergeGraph(state, graph('b', ['b', 'a'], ['first', 'second']));
    expect(flatten(state).nodes.map(n => n.usr)).toEqual(['a', 'b']);
    expect(flatten(state).edges).toHaveLength(2);
    expect(flatten(mergeGraph(state, graph('b', ['b', 'a'], ['first', 'second'])))).toEqual(flatten(state));
  });
  it('does not retain unreachable expansions after collapse, even with a disconnected cycle', () => {
    const batches = { a: graph('a',['a','b']), b: graph('b',['b','c']), c: graph('c',['c','d']), d: graph('d',['d','c']) };
    delete (batches as Partial<typeof batches>).b;
    expect(flatten({ index_id:'snapshot-a', center:'a', batches }).nodes.map(n=>n.usr)).toEqual(['a','b']);
  });
  it('rejects cross-index facts and a graph exceeding the node budget', () => {
    const state = { index_id:'snapshot-a',center:'a',batches:{} };
    expect(() => mergeGraph(state, {...graph('a',['a']), index_id:'snapshot-b'})).toThrow('identities');
    expect(() => mergeGraph(state, graph('a',Array.from({length:BUDGET.nodes+1},(_,i)=>String(i))))).toThrow('budget');
  });
});
