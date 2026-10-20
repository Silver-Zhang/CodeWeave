# CodeWeave map

Node 22.12+, npm. `npm ci && npm run build`; serve `dist` through the local
`codeweave serve --database <index> --frontend frontend/dist` command from the
repository root. For development `npm run dev` proxies `/api` to 127.0.0.1:8000.
No database, fake graph or external code service is embedded in the frontend.

The first successful index-list load selects the newest saved index. Refreshing
the list preserves your selection, graph and source, including history loaded
from older pages. New indexes become options; only an explicit selection switches
snapshots. A failed refresh keeps the current view and offers a retry.

`npm test` checks interaction/state logic; `npm run build` includes strict type
checking. `npx playwright install chromium` then `npm run test:e2e` runs actual
browser integration. Set PYTHON, PYTHONPATH, CODEWEAVE_ANALYZER and optionally
CODEWEAVE_RESOURCE_DIR to a freshly built M0 environment, or use the complete
root `scripts/verify_m1.sh`. Browser fixtures always create disposable real
Clang indexes. HTTP/CLI identities, local budgets and limitations are documented
in `../docs/http-api.md` and the root README.

DOMPurify is overridden to the compatible 3.4.17 security patch because Monaco
0.57.0 pins 3.4.15. Keep the lockfile and audit the override when upgrading.
