## Summary

A collection of bug fixes and UX improvements to `static/index.html` for both the Browse & Generate (single table) and Batch Schema tabs. All changes are frontend-only — no backend API changes.

### Bug fixes

- **Null crash in batch generate**: `desc.replace()` was called on null column descriptions returned by Claude for large tables (e.g., accuweather with 88 columns), causing a JavaScript runtime error and a "Batch failed" popup. Fixed with a `(desc || '').replace(...)` null guard.

- **approveAll / rejectAll skipped table description**: Both functions had an explicit `if (k === '__table__') continue` that prevented the table description from ever being approved or rejected via the bulk buttons. Removed the skip.

- **Batch apply silent failures**: Individual column "Apply" buttons had no loading state — clicking produced a silent 5–10 second wait with no visual feedback. Null column descriptions were also sent to the backend, causing silent per-column failures. Fixed with disabled/textContent state changes during the call, null filtering before the API request, and accurate success/failure counts in the toast.

### UX improvements — Browse & Generate tab

- **Unified description display**: Removed the redundant "Table Description" box that appeared above the AI suggestions section. Table and column descriptions now appear in a single unified list with identical markup — the separate table description box only appears when no AI suggestions have been generated yet.

- **Clear All button**: Added a "↺ Clear All" button alongside Approve All and Reject All that resets all suggestions back to pending state, restoring the original AI-generated text.

### UX improvements — Batch Schema tab

- **Expand All button**: Opens all table cards at once so you can scan all generated descriptions without clicking each header.

- **Apply All Tables button**: Applies every table's descriptions sequentially (waits for each to complete before starting the next), with "Applying..." state during the run and "✓ All Applied" on completion.

- **Column button state during table apply**: When "Apply All for \<table\>" is clicked (either directly or via Apply All Tables), all column "Apply" buttons immediately show "..." (disabled) and flip to "Applied" on success, or revert to "Apply" on failure.

- **Cascading applied state**: When all column "Apply" buttons for a table have been clicked individually, the per-table "Apply All" button automatically updates to "✓ Applied". When all per-table buttons reach applied state (by any path), the global "Apply All Tables" button updates to "✓ All Applied".

## Test plan

- [x] App deployed and verified on a live workspace (fevm-shauver-snap-demo)
- [x] Tested single-table description workflow end-to-end (Browse & Generate tab): generate, approve individual, approve all, reject all, clear all, edit and apply
- [x] Tested bulk schema generation workflow end-to-end (Batch Schema tab): generate for all tables, expand all, apply individual columns, apply all for a table, apply all tables
- [x] Verified cascading applied state: clicking all column buttons individually propagates up to the per-table and global buttons
- [x] Validated that audit log entries are written to the Delta table after applying descriptions in both tabs
- [x] Validated that audit log entries are displayed in the Audit Log tab

This pull request was AI-assisted by Isaac.
