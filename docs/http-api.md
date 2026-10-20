# Local HTTP API (M1 + M2-A)

Start `codeweave serve --database <trusted-sqlite> [--frontend frontend/dist] [--port 8000]`.
Only `127.0.0.1` is bound. Host is localhost/127.0.0.1/[::1]; cross-origin
requests are rejected, including browser origins `null`. No authentication or
network deployment is provided. Use a same-origin built UI or Vite's local proxy.
Database paths cannot be supplied by a client; queries open SQLite with `mode=ro`.
Default serve never builds an index or reads source from the filesystem. M2 workspace refresh is separately enabled by trusted local startup configuration, as described below.

The M1 query endpoints are GET. Success is JSON; errors are
`{"error":{"code":"...","message":"..."}}`. No production traceback.
422 = missing/type-invalid parameters; 400 = invalid range/limit/offset/option;
404 = missing database/index/symbol/registered source; 413 = byte/line budget;
503 = unconfigured, damaged or incompatible database. Unknown routes/methods use
an error object with the appropriate HTTP status. `health` is service capability,
not evidence of a usable index. No responses are cached.

| Route | Parameters | Result |
|---|---|---|
| `/api/health` | none | software capabilities and database configured flag |
| `/api/indexes` | limit=20, offset=0 | saved identities, newest first, creation time/coverage/schema |
| `/api/status` | optional index_id, limit=50, offset=0, details, field, field_offset | exact M0 status summary or bounded provenance details/chunks |
| `/api/search` | **index_id**, q (name/scope substring, SQLite LIKE pattern), limit=20, offset=0 | exact symbol records with USR, declaration/definition, anchors, TU provenance |
| `/api/symbol` | **index_id, usr** | same `items` envelope as CLI show, or symbol_not_found |
| `/api/calls` | **index_id, usr, direction**=callers/callees, limit=20, offset=0 | individual call records with dispatch, instance_id, anchors, units |
| `/api/neighbors` | **index_id, usr**, limit=12 (1..50) | one-hop nodes, individual edges, pages per direction, limits/semantics |
| `/api/source` | **index_id, path, start_line, end_line**, clamp_end=false | saved project snapshot bytes decoded as UTF-8 replacement text |

Apart from status/indexes every query requires an explicit index_id. Select it
from status/indexes, pin it across requests. Indexes are immutable published
observations, not current disk state; unknown/expired IDs fail rather than
falling back. Status/indexes without an ID select the workspace default successful index for schema4, otherwise the last published legacy index. HEAD publication never replaces the Workspace default.
Snapshot source accepts only exact paths registered in that identity, including
when files have changed or disappeared on disk. Authorized dependency identities
without archived content return source_not_registered. No raw-file endpoint. Physical columns remain Clang UTF-8 byte columns; the UI converts valid UTF-8 prefixes to Monaco UTF-16 positions without changing stored facts. Other source encodings are not claimed.
`clamp_end=true` clamps a valid at-most-200-line request at snapshot EOF, useful
for source context. Start beyond EOF is still invalid.

List limit is 1..200, offset is a nonnegative SQLite integer. Search order:
name,USR. Calls: file,line,column,instance_id. Indexes: publication row order,
newest first. Pages include items, returned_count,total_count,remaining_count,
has_more,next_offset,offset,limit,truncated. Pin index_id when paging facts.
Index listing is a changing catalog: new publications can shift its offsets.
Status details have their own M0 row/field chunk contract; see main README.

All shared CLI/HTTP results have a **262144 serialized UTF-8 JSON byte** ceiling
(measured with the CLI serializer including its newline; compact HTTP JSON is
smaller). Oversize facts fail with query_limit; they are not silently clipped.
Status additionally has its stricter 32768-byte budget and recoverable field
chunks. Source is at most 200 lines and 32768 raw bytes before JSON escaping.
A single oversized entity or anchor may be unavailable over a fact query; use
bounded status provenance where applicable. Original facts remain in SQLite.

Neighbors has at most 2*limit call sites and 1+2*limit entities before its byte
budget. Edges retain every original call field and add source/target graph IDs.
Entity IDs are USRs; unresolved indirect targets use `unknown:<instance_id>` and
kind=unknown. A missing indexed external declaration uses kind=external and its
real USR. These are explicit placeholders, never guessed resolved entities.
Virtual edges point to static declarations, not proven runtime targets. Static
calls need not execute on every run. No control-flow trace is claimed.

The server provides no CORS wildcard or third-party script UI. API schema is at
`/openapi.json`; CDN-based Swagger/Redoc pages are disabled. Source/UI content is
rendered as text, not executable HTML. Treat indexes as local trusted startup
configuration; do not expose this service to untrusted networks.


## M2-A configured workspace

`codeweave open --state-dir <trusted-state> --frontend frontend/dist` reopens an
existing local configuration and explicitly enables refresh. First use also
requires trusted project/ccdb/analyzer/resource flags (or `scripts/run_local.sh`).
`serve --database ... --workspace-state ...` remains read-only unless
`--allow-workspace-refresh` is explicitly supplied. No HTTP path/command selection.

| Route | Method / parameters | Result |
|---|---|---|
| `/api/session` | GET | refresh-enabled flag; per-server unpredictable CSRF token only when enabled |
| `/api/workspace` | GET | bounded saved configuration summary, generation/default and last real attempt |
| `/api/workspace/refresh` | POST JSON `{generation,idempotency_token,source_kind?}` | 202 with stable request_id, real phase; source_kind=workspace (default) or head |
| `/api/workspace/attempts/{UUID}` | GET: details=units, limit=20, offset=0; optional field/field_offset | phase/result/error or bounded sorted TU outcomes and command/diagnostic previews |
| `/api/workspace/attempts/{UUID}/cancel` | POST JSON `{}` | real cancellation request; never revokes a committed publication |
| `/api/diff` | GET: **left,right** exact successful IDs | pair identity, scope/provenance, counts per symbol/call/file event kind |
| `/api/diff/{symbols,calls,files}` | GET: **left,right**, limit=20, offset=0, optional change=kind | stable sorted pair-bound event page, remaining_count/has_more/next_offset |
| `/api/source-diff` | GET: **left,right**, left_path/right_path (at least one), left_start=1/right_start=1, lines=40 | saved source windows from both exact identities + unified_diff; absent side null |

Mutations require the bootstrap session's `X-CodeWeave-Token`, application/json,
correct Host and, when supplied, exact same Origin. Token/disabled writes/cross
origin fail403; stale generation, active job or token reused for another payload
fail409. Request body≤4096 bytes; extra keys reject. Identical payload/token is
idempotent, including after completion; it creates one job. Aborting a browser
fetch does not cancel that job. Explicit cancel/restart recovery are separate.
The server listens only on loopback; tokens are a browser request boundary, not
multi-user authentication. Do not expose it through a network proxy.

Phases: queued/capturing/sealed/analyzing/validating/publishing/published,
failed/cancelling/cancelled/interrupted. Worker failure preserves the old default
and its source bytes. Startup recovery only marks stopped owners interrupted;
it never resumes their analysis. Queries remain pinned while jobs run. Polling
reports real phases, not percent estimates. Index catalog refresh preserves the
user's selected historical map; the UI offers an explicit comparison/action.

Workspace/attempt/status summaries≤32768 serialized UTF-8 bytes. Attempt unit
fields over2048 bytes use hashed previews. Fetch full attempt message with
`field=message`; fetch a unit's `command`, `diagnostics` or `source_file` with
`details=units&limit=1&offset=N&field=...&field_offset=B`. Base64 chunks≤8192 raw
bytes with full field hash/size and next byte offset. CLI equivalent:
`workspace attempt UUID --state-dir ... --details units --limit 1 --field command`.
For a published schema4 index, status `field=provenance` or `field=manifest`
uses existing4096-byte chunks, preserving namespace/config/source traceability
without unbounded lists. The full original data remains in SQLite.

Diff summaries≤32768 bytes, event pages≤262144; a page may be shortened for byte
budget (budget_limited), never return an unadvanceable empty partial page.
Per-side source≤200 lines/32768 raw bytes; no live-file fallback. Some namespace
files are manifest-only (not AST-read project buffers); source for those returns
source_not_registered, with no invented text. Authorized dependencies/system
headers retain identity but source is not publicly served. `show/search` schema4
records include definition evidence; calls include syntax evidence. Diff symbol
references include exact byte extents and hashes, call references include exact
site identity and available token evidence. Macro ranges may be null.

Diff distinguishes definition_text_changed, context/location changes, symbols
added/removed/out_of_scope, definition_added/removed and relation counts. A
surviving declaration with lost definition can carry coverage_change=out_of_scope.
Only unique matching normalized function/call token context confirms
`target_changed`; repeats and unsupported macro ranges stay ambiguous/unmatched.
Relations are deterministic static syntax-site multisets, not execution traces.
`definition_context_changed` also compares `unit_files` positive reads joined to
the indexed `files.sha256`, independently of ODRHash. `changed_inputs` identifies
paths and before/after hashes (null for an added/removed read), or a profile pair.
It is a preview of at most8 entries/8192 bytes, with `changed_input_count`,
`changed_inputs_truncated` and the SHA-256 of the complete canonical change list.
`changed_inputs_hash_format=json-sort-keys-compact-ascii-v1` means JSON sorted
object keys, compact comma/colon separators and ASCII Unicode escapes, then UTF-8.
Input identities remain available through paged status inputs; the complete TU
associations remain in SQLite. `odr_hint.authoritative=false` is explicit.
This context is conservatively scoped to observer TUs, excluding definition and
primary source buffers to preserve pure movement/body-edit separation. It is not
a per-definition dependency model: same-buffer external constants can remain
file/source changes only. The comparison envelope states this boundary; absent
function events never prove behavioral equivalence. Comparison policy v2 binds
these semantics into the comparison ID.

Every source window, including each nonnull source-diff side, returns `encoding`
(`utf-8`), `sha256`/`buffer_sha256` for the complete indexed file, `range_sha256`
for the returned raw slice, and zero-based byte offsets `begin`/`end` (exclusive).
Valid UTF-8/CRLF bytes round-trip exactly. A non-UTF-8 window fails closed as
`source_not_utf8` (HTTP422, CLI exit2) with no substituted content; raw stored bytes
are unchanged. A valid window elsewhere in that file remains readable and names
the same full-file hash. A stored buffer/hash mismatch fails
`source_integrity_error` (HTTP503, CLI exit2). Querying never repairs the database
or reads current disk source.
Legacy schema3 (including explicit migrated rows) remains queryable but Diff
fails not_comparable_legacy_index. Different workspace/semantic policy/analyzer
runtime pairs fail indexes_not_comparable. Bad schema/corruption fails with the
normal error object; no arbitrary best-effort facts or fallback identity.

HEAD reads raw Git objects and explicit historical compilation configuration.
Current external dependency roots are shared only as explicitly recorded common
inputs, not falsely labeled historical reconstructions. Missing generated HEAD
inputs block that attempt alone. Git tree equality is required for nonnull
represented_git_commit; base_git_head alone never means saved disk equals Git.
