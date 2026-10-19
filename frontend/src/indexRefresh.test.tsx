import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import App from './App';
import { api, type Entity, type Page } from './api';

vi.mock('./api', () => ({ api: vi.fn() }));
vi.mock('./SourcePanel', () => ({ default: ({ source }: { source: { content: string } }) => <pre>{source.content}</pre> }));
vi.mock('@xyflow/react', () => ({ MarkerType: { ArrowClosed: 'arrow' }, Background: () => null, Controls: () => null,
  ReactFlow: ({ nodes }: { nodes: { id: string }[] }) => <div>{nodes.map(n => <span key={n.id}>node {n.id}</span>)}</div> }));
afterEach(cleanup);
type Index = { index_id: string; created_at: string };
const catalog = (ids: string[], next: number | null = null): Page<Index> => ({ index_id: 'latest',
  items: ids.map(index_id => ({ index_id, created_at: 'now' })), next_offset: next,
  has_more: next !== null, total_count: ids.length, returned_count: ids.length });
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(done => { resolve = done; });
  return { promise, resolve };
}
function setup(list: (offset: number) => Promise<Page<Index>>) {
  vi.mocked(api).mockImplementation(async (route, params) => {
    if (route === 'indexes') return await list(Number(params?.offset)) as never;
    const id = String(params?.index_id);
    const entity: Entity = { index_id: id, usr: 'alpha', name: 'alpha', kind: 'function', definition_file: '/owned/unit.cpp', definition_line: 1 };
    if (route === 'status') return { index_id: id, project_root: '/owned', unit_count: 1, input_count: 1, coverage: 'complete', limitations: [], diagnostic_summary: {} } as never;
    if (route === 'search') return { index_id: id, items: [entity], total_count: 1, has_more: false } as never;
    if (route === 'neighbors') return { index_id: id, center: entity.usr, nodes: [entity], edges: [], pages: {} } as never;
    if (route === 'source') return { index_id: id, path: '/owned/unit.cpp', start_line: 1, end_line: 1, content: `saved ${id}` } as never;
    throw new Error(`unexpected route ${route}`);
  });
  render(<App />);
}
const selected = () => (screen.getByLabelText('Snapshot identity') as HTMLSelectElement).value;
async function choose(id: string, inspect = false) {
  await waitFor(() => expect(screen.getByRole('option', { name: new RegExp(id) })).toBeTruthy());
  fireEvent.change(screen.getByLabelText('Snapshot identity'), { target: { value: id } });
  if (inspect) {
    fireEvent.click(await screen.findByRole('button', { name: /alpha.*function/ }));
    await screen.findByText(`saved ${id}`);
    await screen.findByText('node alpha');
  }
}
function expectSaved(id: string) {
  expect(selected()).toBe(id);
  expect(screen.getByText(`saved ${id}`)).toBeTruthy();
  expect(screen.getByText('node alpha')).toBeTruthy();
}

it('initially selects latest, but refresh preserves a historical graph and source', async () => {
  setup(async () => catalog(['latest', 'history']));
  await waitFor(() => expect(selected()).toBe('latest'));
  await choose('history', true);
  fireEvent.click(screen.getByRole('button', { name: 'Refresh indexes' }));
  await waitFor(() => expect(vi.mocked(api).mock.calls.filter(([r]) => r === 'indexes')).toHaveLength(2));
  await act(async () => {});
  expectSaved('history');
});

it('adds a newly published index without selecting it or discarding saved content', async () => {
  let published = false;
  setup(async () => published ? catalog(['published', 'latest']) : catalog(['latest', 'history']));
  await choose('history', true); published = true;
  fireEvent.click(screen.getByRole('button', { name: 'Refresh indexes' }));
  await screen.findByRole('option', { name: /published/ });
  expectSaved('history');
});

it('retains a selected index loaded beyond the first page, with deduplicated options', async () => {
  setup(async offset => offset ? catalog(['history']) : catalog(['latest'], 20));
  fireEvent.click(await screen.findByRole('button', { name: 'Load older indexes' }));
  await choose('history', true);
  fireEvent.click(screen.getByRole('button', { name: 'Refresh indexes' }));
  await screen.findByRole('button', { name: 'Load older indexes' });
  expectSaved('history');
  expect(screen.getAllByRole('option')).toHaveLength(3); // placeholder plus two identities
});

it('still clears graph and source on an explicit index switch', async () => {
  setup(async () => catalog(['latest', 'history']));
  await choose('history', true);
  await choose('latest');
  await waitFor(() => expect(screen.queryByText('node alpha')).toBeNull());
  expect(screen.queryByText('saved history')).toBeNull();
  await waitFor(() => expect(vi.mocked(api).mock.calls.some(([r, p]) => r === 'status' && p?.index_id === 'latest')).toBe(true));
});

it('does not overwrite a manual selection made while a refresh is pending', async () => {
  const pending = deferred<Page<Index>>(); let calls = 0;
  setup(async () => ++calls === 1 ? catalog(['latest', 'history', 'other']) : pending.promise);
  await choose('history', true);
  fireEvent.click(screen.getByRole('button', { name: 'Refresh indexes' }));
  await choose('other');
  await act(async () => { pending.resolve(catalog(['published', 'latest'])); });
  expect(selected()).toBe('other');
  expect(screen.queryByText('saved history')).toBeNull();
  expect(screen.getByRole('option', { name: /published/ })).toBeTruthy();
});

it('ignores an earlier catalog response after a later refresh has completed', async () => {
  const pending = deferred<Page<Index>>(); let calls = 0;
  setup(async () => ++calls === 1 ? catalog(['latest', 'history']) : calls === 2 ? pending.promise : catalog(['published', 'latest']));
  await choose('history', true);
  fireEvent.click(screen.getByRole('button', { name: 'Refresh indexes' }));
  fireEvent.click(screen.getByRole('button', { name: 'Refresh indexes' }));
  await screen.findByRole('option', { name: /published/ });
  await act(async () => { pending.resolve(catalog(['obsolete'])); });
  expectSaved('history');
  expect(screen.queryByRole('option', { name: /obsolete/ })).toBeNull();
  expect(screen.getByRole('option', { name: /published/ })).toBeTruthy();
});

it('preserves a valid view through refresh failure and explicit retry', async () => {
  let fail = false;
  setup(async () => { if (fail) throw new Error('database_error: owned failure'); return catalog(['latest', 'history']); });
  await choose('history', true); fail = true;
  fireEvent.click(screen.getByRole('button', { name: 'Refresh indexes' }));
  expect((await screen.findByRole('alert')).textContent).toContain('database_error');
  expectSaved('history'); fail = false;
  fireEvent.click(screen.getByRole('button', { name: 'Retry indexes' }));
  await waitFor(() => expect(screen.queryByRole('alert')).toBeNull());
  expectSaved('history');
});

it('handles initial empty database and selects the first index after retry', async () => {
  let empty = true;
  setup(async () => { if (empty) throw new Error('index_not_found: no matching index'); return catalog(['latest']); });
  expect((await screen.findByRole('alert')).textContent).toContain('index_not_found');
  expect(selected()).toBe('');
  expect((screen.getByLabelText('Function or method') as HTMLInputElement).disabled).toBe(true);
  empty = false; fireEvent.click(screen.getByRole('button', { name: 'Retry indexes' }));
  await waitFor(() => expect(selected()).toBe('latest'));
  expect(screen.queryByRole('alert')).toBeNull();
});
