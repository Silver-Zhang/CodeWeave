# CodeWeave — source repository rules

This is the public MIT source repository. Keep it independently buildable and testable.
Do not require access to any workflow repository to use, build, or contribute to it.

- Store implementation, regression tests, build configuration, stable API specifications
  and user-facing documentation here. Keep plans, task reports and raw execution logs outside.
- This product is an automatically generated code map, not a chat/notes knowledge base.
  Human UI and agent queries must use the same index and source identity.
- Preserve source snapshots, build-profile identity, precise anchors, call sites, coverage
  and unknown dispatch information. Never turn a reference into a proven call or a
  static path into an observed execution sequence.
- Treat imported repositories as read-only. Never build/run or modify imported source,
  refresh its Git index, checkout/reset/clean, or upload it without authorization.
- Never commit credentials, private inputs, indexes, production snippets or
  machine-specific paths. Public screenshots and test artifacts need a privacy check.
- Work within the assigned scope. Use focused tests, preserve other changes, report
  exact commands, outputs and limitations. Do not force-push, merge, release or change
  repository visibility without authorization.
- Report bootstrap, indexing, UI and validation capabilities separately.
  A health-check success is not proof of a working analyzer.

The current checkout contains only a bootstrap.
