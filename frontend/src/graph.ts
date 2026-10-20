import type { Call, Entity, Graph } from './api';
export const BUDGET = { nodes: 100, edges: 150, requests: 24 };
export interface MapState { index_id: string; center: string; batches: Record<string, Graph> }
export function mergeGraph(state: MapState, graph: Graph): MapState {
  if (graph.index_id !== state.index_id) throw new Error('Cannot mix index identities');
  const next = { ...state, batches: { ...state.batches, [graph.center]: graph } };
  const visible = flatten(next);
  if (visible.nodes.length > BUDGET.nodes || visible.edges.length > BUDGET.edges) throw new Error('Map budget reached. Collapse a branch or reset before expanding.');
  return next;
}
export function flatten(state: MapState): { nodes: Entity[]; edges: Call[] } {
  const nodes = new Map<string, Entity>(); const edges = new Map<string, Call>();
  // Only batches reachable from the root survive collapsing a branch, including cycles.
  const reachable = new Set([state.center]); const queue = [state.center];
  for (let i = 0; i < queue.length; i++) {
    const batch = state.batches[queue[i]]; if (!batch) continue;
    for (const node of batch.nodes) { nodes.set(node.usr, node); if (!reachable.has(node.usr)) { reachable.add(node.usr); queue.push(node.usr); } }
    for (const edge of batch.edges) edges.set(edge.instance_id, edge);
  }
  return { nodes: [...nodes.values()], edges: [...edges.values()] };
}
// A deterministic grid handles cycles without treating edges as execution order.
export function position(index: number) { return { x: (index % 3) * 310, y: Math.floor(index / 3) * 130 }; }
