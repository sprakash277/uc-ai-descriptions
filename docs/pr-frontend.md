## Summary

A collection of bug fixes and UX improvements to `static/index.html` for the Browse & Generate, Batch Schema, and Responsible AI Rules tabs. Includes a new per-description regeneration feature backed by `POST /api/generate/item` (in the companion backend PR).

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

### Per-description regeneration

- **Browse tab**: Each suggestion row (table + every column) now has a **↺ Regen** button. Clicking it calls `POST /api/generate/item` for just that item, updates only that row, and resets its status to pending — all other rows (and their approve/reject state) are preserved.

- **Batch tab**: Each column row and the table description row have a **↺** button. Clicking regenerates that item in place: the description cell updates, `batchResults` state is synced, and the Apply button's description is refreshed so the regenerated text is what gets applied.

### Responsible AI Rules tab redesign

- **Currently effective rules**: The main rules display now shows whichever rules are actually in effect — either the session override or the org defaults from `config.yaml` — with a source badge ("Session override active" in orange, or "System default (config.yaml)" in dim text).

- **Session override workflow**: Per-session rule overrides (entered via the session rules panel) now propagate immediately to the Rules tab display. When an override is active, the org rules are shown below in a dimmed "System Rules" section for reference.

## Test plan

Testing was performed on a live AWS workspace (fevm-shauver-snap-demo).

- [x] App deployed and verified end-to-end on a live AWS workspace
- [x] Tested single-table description workflow (Browse & Generate tab): generate, approve individual, approve all, reject all, clear all, edit, apply; per-item regenerate on table and column descriptions
- [x] Tested bulk schema generation workflow (Batch Schema tab): generate for all tables, expand all, apply individual columns, apply all for a table, apply all tables; per-item regenerate on column and table descriptions
- [x] Verified cascading applied state: clicking all column buttons individually propagates up to the per-table and global buttons
- [x] Verified session rules override: enter override, confirm Rules tab shows orange "Session override active" badge and correct text; clear override, confirm revert to org rules
- [x] Validated that audit log entries are written to the Delta table after applying descriptions in both tabs
- [x] Validated that audit log entries are displayed in the Audit Log tab

This pull request was AI-assisted by Isaac.
