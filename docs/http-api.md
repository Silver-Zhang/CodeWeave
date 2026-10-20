# Local HTTP API (M1)

Start `codeweave serve --database <trusted-sqlite> [--frontend frontend/dist] [--port 8000]`.
Only `127.0.0.1` is bound. Host is localhost/127.0.0.1/[::1]; cross-origin
requests are rejected, including browser origins `null`. No authentication or
network deployment is provided. Use a same-origin built UI or Vite's local proxy.
Database paths cannot be supplied by a client; queries open SQLite with `mode=ro`.
HTTP never builds an index or reads source from the filesystem.

All endpoints are GET. Success is JSON; errors are
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
falling back. Status/indexes without an ID select the latest successful index.
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
