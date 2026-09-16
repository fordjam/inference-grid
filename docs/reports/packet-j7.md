# Glm-5.3-Flash lane report — brief 14, J7: a brief becomes board work — `board-new --from-brief` (2026-09-15)

Lane: Glm-5.3-Flash (packet lane). Base: `24e968e` (the tip of `glm/work` at branch
time). Branch: `packet/packet-j7`, one commit, not pushed.

## Why

Every repository's backlog lives in a handoff brief, but the board holds only what
someone authored by hand, and "is this item done?" is answered differently in each
repository — a report file here, a CONTRIBUTIONS row there, a commit subject in a third.
The coordinator swept six repositories by hand and got two wrong. The sweep should be a
command: read the brief's `#### <id>. <title>` packets, author a `packet` task for every
item no evidence says is done, and report the rest with the evidence that decided them.

## What landed

**`src/inference_grid/board/from_brief.py`** (new module; nothing provider-authored
touched)

- `from_brief(board_dir, project_root, from_brief, lanes, gates, base, boards_dir,
  dry_run)` reads the brief document (absolute path, or relative to `project_root`),
  walks `lanes/brief.py::PACKET_HEADING` (`#### <id>. <title>`, ids `[A-Z]\d+`), and
  authors one `packet` task per undone item: id `packet-<id-lower>`, brief
  `grid/briefs/packet-<id>.txt`, artifacts `["docs/reports/packet-<id>.md"]` (the report
  name the packet loop's own prompt asks the lane to write), the packet budget
  `plan_task.PACKET_BUDGET` (3600 s / 10 MB / 6000 thinking — the same numbers every
  hand-authored packet task carries), state `ready`. Every task is validated through
  `validate_packet_task` before a single file is written, so a bad item refuses the
  whole sweep rather than half-authoring it.
- **Done signals.** An item is done when any of: a `docs/reports/*<id-lower>*.md` file
  exists; a `docs/CONTRIBUTIONS.md` row begins `| <id> |`; or a commit since the
  brief's own commit (the commit that added the brief file, found by `git log
  --diff-filter=A`) has a subject pairing the id with the brief's number. The number
  comes from the brief file name's trailing digits, else the title's `brief <n>`; a
  brief without a number simply has no commit signal. Subjects match the three shapes
  the packet names — `brief 16, I2`, `(brief 14 A1)`, `[handoff-6/B1]` — with a colon
  after the number (`brief 17: J6 — …`) accepted as the first shape's separator; an id
  without the number, or under another brief's number, decides nothing, and nothing
  before the brief's own commit counts. Every git read goes through one module-level
  `run` seam (the `branch_review.py` pattern).
- **Two CONTRIBUTIONS row shapes**, disclosed: the specified `| <id> |` prefix, plus the
  variant this repository's own brief-14 tables actually write, where the lane names
  the item (`| Glm-5.3-Flash (J5, branch ...`). Without the variant, the sweep would
  judge this repository's own landed rows not-done — the exact class of error the
  packet was written to end.
- **The task brief** is `hard_rules(brief)` (the umbrella's `## 1. Hard rules`, closed
  by `---`, exactly as `lanes/brief.py` cuts it) + `---` + the item's own section — the
  composition a hand-authored packet brief uses, verified by `test_packet_task`'s
  shape. A brief whose rules section lacks the closing `---` is refused *before
  anything is written*, with the line to add (`add a line reading --- ...`) — a refusal
  `hard_rules` itself cannot give, since it raises the same bare message for a missing
  section and a missing closer.
- **Gates.** The caller's `gates` list, else the repository's declared test command as
  one `tests` gate read from `test_argv` in the tick config (the config in
  `boards_dir` whose `board_dir` matches — so the call grew a `boards_dir` key the
  packet's JSON sketch did not name; the tick config is not reachable from `board_dir`
  alone and home paths stay out of source). A tick config that names no `test_argv`
  refuses, naming both fixes (declare it, or pass `gates`). The commit gate is never
  authored into the spec: `board/packet_task.py::_gates_for` appends it to every packet
  task at dispatch, so "the commit gate always" holds without a second commit gate
  running per round — authoring it would run commit twice.
- **`base`** defaults to the project's current branch (the packet's packets branch from
  a work base the caller knows better; the operator passes `base` when it matters).
  **`lanes` is required**: a packet dispatches only on packet-capable lanes
  (`goat_cli`/`zcode_cli`/`cline_cli` kinds), and no default can know which lane ids
  those are without reading `lanes.json`, which this package never touches. Absent
  `lanes` refuses with that reason. This is a deviation from the packet's `lanes?`
  sketch, recorded here for the operator.
- **Idempotence.** An item whose board task file or brief file already exists is
  skipped (listed in the reply with the path), not authored and not a refusal — a
  re-run over a grown brief is safe. `dry_run` returns the full plan (authored, done,
  skipped) and writes nothing.

**`src/inference_grid/cli.py`** — `board_new` grew `from_brief`, `gates`, `base`,
`boards_dir` and `dry_run`, routed to the new module after the `project_root` check;
the CLI round trip is the same `--json` file every other board-new path uses.

## Tests

`tests/test_from_brief.py` (14 cases, all offline — the sandbox here denies `git`
itself, so the module's `run` seam is faked; the files are real):

- the three done signals each recognised (report file, `| <id> |` row, commit subject
  since the brief's own commit), plus the lane-named CONTRIBUTIONS variant;
- the commit subject shapes: comma, colon and bare-space forms, the `[handoff-n/id]`
  form, an id without the number, another brief's number, and nothing before the
  brief's own commit or without one;
- an undone item authored with the right lanes, gates, base, inputs, artifacts and
  spec — and the board loads it through `runner.load_board`'s validation; the composed
  brief starts with the rules section, carries the item and not the whole phase;
- the rules section without the closing `---` refused with the line to add;
- gates defaulted from the tick config's `test_argv`, and a config without one refused
  naming `test_argv`;
- `dry_run` writes nothing; a re-run skips what the board already holds; `lanes`
  missing refuses; the CLI round trip through `cli.main()`.

## Verification

- Full suite, `--continue-on-collection-errors`: **88 failed, 580 passed, 7 skipped,
  1 error** — the `FAILED`/`ERROR` node-id set **byte-identical** to a scratch-worktree
  run of the base `24e968e` (`diff` empty; 88 pre-existing sandbox refusals + the
  calibration deny-read collection error, zero new, zero repaired), passes 566 → 580,
  the fourteen new cases being the whole difference. (One earlier run of this same
  tree printed 579 passed with the failure set identical — a run where one test's
  outcome went unreported; the collected count here is exact, 661 + 14 = 675. The
  gate's own pytest invocation stops at the same collection error, as the J6 report
  already recorded; this lane's git is also unavailable in-shell — the Xcode license
  gate — which is why the module's git seam is injected and faked in tests.)
- `ruff format --check` and `ruff check` clean on the three changed files, scoped as
  `lanes/gates.py::run_scoped_ruff` scopes them. (A first `ruff format src tests`
  reformatted 24 untouched files under this venv's ruff version; every one was
  reverted byte-for-byte before this commit — the repo is deliberately not
  format-clean repo-wide, as the gate's own docstring records.)

## Two things worth the operator's eye

- **`lanes` is required, not optional as sketched.** A sensible default would need
  `lanes.json` (or a tick-config key naming packet lanes). If the sweep is to run
  unattended, a `packet_lanes` key in the tick config would be the credential-free
  place for it.
- **The report-file signal is a substring match** (`docs/reports/*<id>*.md`), as the
  packet specifies. An id that is a substring of another's (`J1` inside `packet-j1` vs
  a hypothetical `…-j10` — not possible with the `[A-Z]\d+` heading rule, but `A1`
  matches `…-sha1-report.md`) would over-match; none exists today.
