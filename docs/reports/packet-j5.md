# GLM lane report — brief 17, J5: the boards on the local dashboard (2026-09-15)

Lane: GLM-5.3-Flash (packet lane). Base: `340cb71` (the tip of `glm/work` at branch
time; its own commit only wrote the J5 brief). Branch: `packet/packet-j5`, one commit,
not pushed.

## Why

The operator could see planned and active work only by reading board JSON and the ledger
by hand. `inference-grid digest` is markdown with counts, and it listed a board twice when
a config directory held two files for one project. J5 turns the same read-only facts into
a data node the local dashboard can render: what is planned, what is running, what is
blocked (and on whom) and what landed today — with every row legible without knowing what
`packet-q5` or `M2` means.

## What landed

**`src/inference_grid/boards.py`** — the data node. `boards(ledger, boards_dir=…, boards=…,
database=…, packets_root=…, now=…)` returns one dict per board,
`{name, planned, active, blocked, landed_today}`:

- *planned* is the dry-run plan row for each `ready` task. The plan comes from
  `board.runner.tick(..., dry_run=True)`, which stops at route: no attempt is created, no
  task file is written, no packet directory is touched. `plan_reason` maps route's
  vocabulary to the three the dashboard names — `lane_busy` (or every candidate busy) →
  `account busy`, a stale candidate → `quota stale`, no candidate and no drop row →
  `no_lane_for_category` — and carries any other reason through as reported.
- *active* is each `dispatched` task's live attempt: found by matching the ledger's ACTIVE
  rows whose workspace sits under `<packets_root>/<task id>/`, so the lane comes from the
  attempt's account and the model from the ledger task's spec. The round is the newest
  `gates-*` directory (at least 1 while the first round runs) out of the task's
  `max_rounds`, minutes elapsed is measured from the attempt's newest own file (never its
  `work/` clone), and the gate is the newest `gates-*` directory's last `gate-*.log` tail.
- *blocked* carries the reason and `operator_owed`, computed from
  `operator_queue.OPERATOR_REASON_WORDS` with `EXCLUDED_REASON_WORDS` (`superseded`)
  excluded — the same rule the needs-you overlay uses.
- *landed_today* is the tasks settled `landed` or `passed` whose file was written since
  local midnight.

Every row carries `project` (the board name), `id`, `title`, `focus`, the full `section`
(the dashboard's drawer shows it) and `links`. `brief_identity` reads the packet heading
`#### <ID>. <title>` from the task's brief by `lanes/brief.py`'s own `PACKET_HEADING`
pattern — the heading the task's `spec.packet_id` names, else the first — and the section's
first sentence as the focus; a brief with no heading reports the task id. Links are the
brief file, the newest `docs/reports/*<id>*.md` (the id lowercased, since report files are)
and, for active rows, the attempt directory. Only the brief and the filesystem are read;
nothing is fetched. Missing lanes file, unreadable brief or an uninitialized ledger degrade
to an empty plan / no links instead of failing the read.

**`src/inference_grid/cli.py`** — `boards` joins the command list (JSON required) and
`boards_command` dispatches to the node.

**`src/inference_grid/digest.py`** — the duplicate fix: a config directory with two files
for one project now yields one `## Board <name>` entry (deduped by project name, first
config wins), so a duplicated board is no longer listed twice with identical counts.

**`src/inference_grid/capacity.py`** (mirrored byte-for-byte to
`deployments/capacity/capacity.py`, which CI `cmp`s) — `clean_boards` sanitizes the new
section (fixed row shape, every string bounded, rows without an id dropped), `project`
carries it as `boards`, and the server answers `GET /api/boards` from the overlay alone —
independent of the upstream quota feed, so the Boards section renders even when the feed
is down.

**`deployments/local/overlay_build.py`** — `boards_section(config)` calls the node with the
config's `boards` list (the same `{name, board_dir, …}` rows the overlay already carries),
threading the top-level `lanes_path`/`packets_root` into each entry, and writes the result
under `boards` in `overlay.json`. Any failure yields `[]`: the overlay must still be
written.

**`src/inference_grid/capacity_web/`** — a **Boards** section (`app.js`, `index.html`,
`app.css`): one card per board with the four counts in the header, the four lists beneath
(running first), each row showing the title first with the id as a small badge and the
project name on the card. Clicking (or Enter on) a row opens a drawer with the focus
sentence, the full packet section, lane / model / round, the last gate tail and the links.
Above the cards an **All work** table lists every row across boards with columns project ·
title · state · lane · age, sortable by any column and filterable by project and state.
No new dependency; the page's existing theme tokens and helpers (`node`, `duration`) are
reused. The service-worker cache is bumped `v4` → `v5` so the fixed shell is fetched.

**Local only.** `deployments/local/upload.py` builds its body from the account allowlist,
so `boards` never leaves the machine; `deployments/capacity/cloud_server.py::clean_snapshot`
raises on a `boards` key outright rather than stripping it, because the rows carry task ids
and titles — the operator's own project names.

## Tests

`tests/test_boards.py` (8 tests, all offline — a temp board, a temp sqlite ledger and a fake
attempt directory; no lane runs, nothing is dispatched):

- `test_snapshot_lists_one_task_in_each_state` — one task in each state: the ready packet's
  plan row names lane `go` with no reason, title `Some title` and focus `The first sentence
  states the work.`; the ready task with no heading reports its id; the dispatched task
  reports lane/model, round 1 of 3, the `tests` gate tail and the attempt directory; the
  blocked row is operator-owed; `landed_today` holds the landed task.
- `test_snapshot_is_read_only` — every board file is byte-identical afterwards.
- `test_brief_identity_reports_the_heading_and_its_first_sentence` — the heading title and
  first sentence; the no-heading case reports the id; an unreadable brief invents nothing.
- `test_plan_reason_names_the_three_refusals` — `account busy`, `quota stale`,
  `no_lane_for_category`, and an unnamed reason carried through.
- `test_digest_lists_a_board_once` — two config files for one project, one digest entry.
- `test_project_carries_and_sanitizes_boards` — the boards section survives `project`,
  unknown keys are stripped, malformed sections invent nothing.
- `test_the_dashboard_renders_the_boards_section` — `app.js` references `/api/boards`,
  renders `title` and names the All work table; the HTML carries the section, cards, table
  and drawer hosts.
- `test_clean_snapshot_refuses_boards` — the cloud boundary raises on a `boards` key.

`tests/test_local_collectors.py::UploadTests::test_boards_never_leave_the_machine` — a feed
carrying `boards` produces a body with no `boards` key and no task id.
`tests/test_digest.py` gained the duplicate case; `deployments/capacity/test_cloud.py` gained
`test_boards_rejected` (run by CI's cloud step, outside `testpaths`).

## Verification

- Full suite with `--continue-on-collection-errors`: **88 failed, 552 passed, 7 skipped,
  1 error** (the `tests/test_calibration.py` deny-read collection error). The 89
  `FAILED`/`ERROR` node ids are **byte-identical** to a scratch worktree at the base
  `340cb71` (`diff` empty both ways): the pre-existing sandbox refusals
  (`killpg`/`/bin/ps`/`sandbox-exec` denials), zero new, zero repaired. Passes 541 → 552.
- `deployments/capacity/test_cloud.py`: **26 passed** (`python -m unittest -v test_cloud`).
- `ruff format` and `ruff check` clean on every changed Python file; `cmp
  src/inference_grid/capacity.py deployments/capacity/capacity.py` identical.
- `node --check src/inference_grid/capacity_web/app.js` clean.
- No provider-authored file was touched (`observation.py`, `goat_outcomes.py`,
  `remaining_units.py`, `provider_report.py`, `lane_readiness.py`, `flash_window.py`,
  `lanes/config.py`, `lanes/select.py`, `lanes/sandbox.py`, `board/task.py` all unchanged).
  No new credential access; no absolute home path or e-mail in the diff.
