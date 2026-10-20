# Saved-workspace demo

The launcher copies this owned example into your state directory **once** and
creates a small local Git HEAD and compilation database. Reopening preserves
your changes. The demo is parsed by real Clang; it is never built or executed.

1. Search `demo::run`, open its map, click a node or call site for saved source.
2. In the copied `main.cpp`, change `literal()` from `return 7` to `return 9`.
   In `choose()`, change `return alpha()` to `return beta()`. Save.
3. Click **刷新工作区**, wait for `published`, then **比较上次成功与本次结果**.
   Function changes include `definition_text_changed`; call changes include
   `relation_added`, `relation_removed` and the unique `target_changed`.
4. Click a change for real two-sided saved-source Diff, or inspect it in either
   version's map. Refresh does not replace the historical map you selected.
5. Prepend blank lines to `main.cpp` and refresh: location may change, but call
   relations/counts must not change. Change `config.hpp`'s scale to 5 to exercise
   a macro-only header. Restore valid syntax after trying a compile error;
   failed refreshes preserve every previous successful index.
6. **建立 HEAD 基线** reads the demo's actual Git objects and committed command
   configuration. It never resets your edited files. Select HEAD and workspace
   as the comparison pair. Restart with the same state directory for history.

Changes inside definition ranges include text changes (even whitespace), not a
proof of changed runtime behavior. Static virtual targets and unknown indirect
targets retain their original meanings.
