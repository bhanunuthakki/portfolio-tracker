# Portfolio Tracker interface contract

This is the project-owned visual and interaction authority for the retained local operations
console. Read it before a material visible change. The shared frontend procedure owns general
composition and accessibility; this file owns Portfolio Tracker's product posture and executable
visual sources.

## Product outcome

Help the owner inspect and repair portfolio data with clear provenance and operational state. The
frontend is not the primary investing-analysis workspace: Earnings Summary owns the consolidated
investment experience, while Portfolio Tracker retains connections, sync operations, source
inspection, transactions, data quality, corrections, database health, and API visibility.

The currently shipped navigation is Accounts, Holdings, and Transactions. Until the planned
surface migration is implemented, improve those routes without introducing a competing analytical
dashboard. A target-state design document is not evidence that a route has already moved.

## Visual authority

- `frontend/src/index.css` owns semantic color variables, light and dark themes, global type,
  numeric alignment, focus treatment, and base component classes.
- `frontend/tailwind.config.js` maps Tailwind names to those semantic tokens.
- `frontend/src/components/ui.tsx` owns reusable panels, controls, pills, figures, and table rhythm.
  Add a shared variant there instead of repeating page-local presentation.
- Page modules own task composition but may not create a second token or component system.

The visual language is a dense editorial operations console: warm paper surfaces, quiet hairline
rules, one interaction accent, tabular numeric figures, and disciplined gain/loss/warning states.
Use tables and aligned grids for repeated records. Reserve cards for a real state, interaction, or
ownership boundary.

## Interaction and data states

- Put provenance, as-of time, account coverage, and stale or partial-ingest warnings beside the
  affected data rather than in a detached disclaimer.
- Distinguish idle, running, partial, successful, failed, and stale states. Never preserve an old
  value in a way that makes a failed refresh appear current.
- Preview migrations, backfills, corrections, relinks, and destructive operations with affected
  accounts, dates, and row counts before the user confirms them.
- Keep the common operational action visible. Put diagnostic detail and uncommon controls behind
  labeled progressive disclosure.
- Every data table uses the shared sortable-header behavior for all columns; editable configuration
  grids are the documented exception.
- Preserve visible keyboard focus, native control semantics, and readable overflow at narrow
  widths. Light and dark themes must communicate the same semantic states.

## Required evidence

For a material frontend change, render the affected route and its closest shipped sibling at
1440 × 900, then inspect the affected task at 390 × 844. Exercise the primary interaction and
its failure or empty state, check keyboard focus and overflow, and inspect browser errors. Run the
TypeScript and production-build gate named in `AGENTS.md`; report any state that could not be
exercised with real or fixture data.
