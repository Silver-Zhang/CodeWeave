export interface Entity { index_id: string; usr: string; name: string; kind: string; definition_file?: string; definition_line?: number; definition_column?: number; declaration_file?: string; declaration_line?: number; declaration_column?: number; anchors?: unknown; units?: string[] }
export interface Call { index_id: string; instance_id: string; caller_usr: string; target_usr: string | null; dispatch: string; file: string; line: number; column_number: number; source: string; target: string; anchors: unknown; units: string[] }
export interface Page<T> { index_id: string; items: T[]; has_more: boolean; next_offset: number | null; total_count: number; returned_count: number }
export interface Graph { index_id: string; center: string; nodes: Entity[]; edges: Call[]; pages: Record<string, { has_more: boolean; total_count: number }> }
export interface Status { index_id: string; project_root: string | { preview: string }; coverage: string; unit_count: number; input_count: number; limitations: string[]; diagnostic_summary: Record<string, number> }
export interface Source { index_id: string; path: string; start_line: number; end_line: number; content: string }
export async function api<T>(route: string, params: Record<string, string | number> = {}, signal?: AbortSignal): Promise<T> {
  const timeout = AbortSignal.timeout(15000);
  const joined = signal ? AbortSignal.any([signal, timeout]) : timeout;
  let response: Response;
  try { response = await fetch(`/api/${route}?${new URLSearchParams(Object.entries(params).map(([k,v]) => [k, String(v)]))}`, { signal: joined }); }
  catch (error) { if (signal?.aborted) throw error; throw new Error(timeout.aborted ? 'Request timed out. Retry or narrow the query.' : 'Cannot reach the local API. Check the server and retry.'); }
  const body = await response.json();
  if (!response.ok) throw new Error(`${body.error?.code ?? response.status}: ${body.error?.message ?? 'Request failed'}`);
  if (params.index_id && body.index_id !== params.index_id) throw new Error('Index identity mismatch; reload the selected index.');
  return body;
}
