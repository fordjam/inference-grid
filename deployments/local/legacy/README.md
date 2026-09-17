# Legacy scripts carried over from yt-research-mcp (05-A3, 2026-09-17)

Under plan row 05-A3, `yt-research-mcp/scripts/` was split against
`plans/reports/2026-09-17-goat-yt-inventory.md`'s file-by-file verdicts. See
`../../docs/MIGRATION_YT_RESEARCH.md` for the mapping this migration followed.

- `alignment_check.sh` — the only file actually moved here (`git rm`'d from
  yt-research-mcp once this copy landed). Prints cross-repo branch/upstream/dirty state
  across the Air and the mini. No Grid equivalent exists; not wired into the Grid's own
  runner, boards or collectors. Its `repos=(...)` array still names the old
  `~/MyMonarch`/`~/vix-rs`/`~/projects/yt-research-mcp` paths — update them before running.

`board_audit.py` and `capacity_report.py` were assessed for the same move (their inventory
verdict also reads "inference-grid") but stayed in `yt-research-mcp/scripts/` instead:
`programme_status.py` there (`from board_audit import audit as audit_board`,
`import capacity_report`) hard-imports both by module name from the same directory, and
`programme_status.py` itself is staying in yt-research-mcp until plan row 05-A2 moves it to
Quant Factory. Deleting either now breaks `tests/test_programme_status.py`. Per the migration
doc, `board_audit.py` still needs to be re-pointed at the Grid's own `grid/board` directory
and re-read against `board.task`'s validation before it is trusted here, and `capacity_report.py`
is a reader of the *old* yt-research-mcp ledger, kept until the Grid's collector covers the
same seats. Revisit both once 05-A2 lands.

Also assessed and kept in `yt-research-mcp/scripts/` for the same reason: `capacity.py`,
`codex_capacity.py`, `commandcode.py`, `capacity_snapshot.py` — `runner.py` there imports
`capacity` and `codex_capacity` directly, and `capacity.py` in turn imports `commandcode` and
`capacity_snapshot`; moving any one of the four without the others would break `runner.py`
and its pinned test suite (`tests/test_jobs.py`). See the 05-A3 land request in
`plans/reports/2026-09-17-yt-scripts-out.md` for the full reasoning.
