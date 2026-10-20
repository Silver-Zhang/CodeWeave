export interface Entity { index_id: string; usr: string; name: string; kind: string; definition_file?: string; definition_line?: number; definition_column?: number; declaration_file?: string; declaration_line?: number; declaration_column?: number; anchors?: unknown; units?: string[] }
export interface Call { index_id: string; instance_id: string; caller_usr: string; target_usr: string | null; dispatch: string; file: string; line: number; column_number: number; source: string; target: string; anchors: unknown; units: string[] }
export interface Page<T> { index_id: string; items: T[]; has_more: boolean; next_offset: number | null; total_count: number; returned_count: number }
export interface Graph { index_id: string; center: string; nodes: Entity[]; edges: Call[]; pages: Record<string, { has_more: boolean; total_count: number }> }
export interface Status { index_id: string; project_root: string | { preview: string }; coverage: string; unit_count: number; input_count: number; limitations: string[]; diagnostic_summary: Record<string, number> }
export interface Source { index_id: string; path: string; start_line: number; end_line: number; content: string; encoding?: string; sha256?: string; buffer_sha256?: string; range_sha256?: string; begin?: number; end?: number }
export interface IndexEntry { index_id: string; created_at: string; source_kind?: string; snapshot_id?: string }
export interface Attempt { request_id: string; state: string; source_kind: string; result_index_id: string | null; error_code: string | null; message: string | { preview: string }; unit_count: number }
export interface WorkspaceStatus { configured: boolean; refresh_enabled: boolean; generation: number; project_root: string; latest_successful_index: string | null; last_attempt: Attempt | null }
export interface EvidenceRef { index_id: string; usr?: string; name?: string; caller_usr?: string; target_usr?: string | null; file: string; line: number; column: number; instance_id?: string; dispatch?: string }
export interface ChangeEvent { event_id: string; kind: string; before: EvidenceRef | null; after: EvidenceRef | null; caller_usr?: string; target_usr?: string; before_count?: number; after_count?: number; confidence?: string; match_method?: string; changed_inputs?: Record<string, unknown>[]; changed_input_count?: number; changed_inputs_truncated?: boolean; changed_inputs_sha256?: string; changed_inputs_hash_format?: string; odr_hint?: unknown; context_scope?: string }
export interface Comparison { comparison_id: string; left_index_id: string; right_index_id: string; counts: Record<string, Record<string, number>>; configuration_changed: boolean; scope: string; context_scope?: string; context_limitations?: string[]; left: {source_kind: string; provenance: unknown}; right: {source_kind: string; provenance: unknown} }
export interface ChangePage { comparison_id: string; left_index_id: string; right_index_id: string; items: ChangeEvent[]; returned_count: number; total_count: number; next_offset: number | null; has_more: boolean }
export interface SourceDiff { comparison_id: string; left_index_id: string; right_index_id: string; left: Source | null; right: Source | null; unified_diff: string; windowed: boolean }
export async function api<T>(route: string, params: Record<string, string | number> = {}, signal?: AbortSignal): Promise<T> {
  const timeout = AbortSignal.timeout(15000);
  const joined = signal ? AbortSignal.any([signal, timeout]) : timeout;
  let response: Response;
  try { response = await fetch(`/api/${route}?${new URLSearchParams(Object.entries(params).map(([k,v]) => [k, String(v)]))}`, { signal: joined }); }
  catch (error) { if (signal?.aborted) throw error; throw new Error(timeout.aborted ? 'Request timed out. Retry or narrow the query.' : 'Cannot reach the local API. Check the server and retry.'); }
  const body = await response.json();
  if (!response.ok) throw new Error(`${body.error?.code ?? response.status}: ${body.error?.message ?? 'Request failed'}`);
  if (params.index_id && body.index_id !== params.index_id) throw new Error('Index identity mismatch; reload the selected index.');
  if (params.left && (body.left_index_id !== params.left || body.right_index_id !== params.right)) throw new Error('Comparison identity mismatch; reselect versions.');
  return body;
}
