# GLM lane report — brief 17, J1: the plan node (2026-09-15)

Lane: GLM-5.3-Flash via Command Code. Base: `glm/work` at `6277de4`.
Branch: `packet/packet-j1`, one commit, not pushed.

## Why

Nineteen packets have landed and the board runs, gates, lands and measures on its own.
The one thing still only the coordinator produced was the brief itself: every packet so
far was hand-written from a reading of the tree that a program could have taken. The
facts a brief opens with — which files exist, which commits last touched them, which
tests pin them — are already a code node (`lanes/scout.py::orient`). This packet puts a
`plan` task in front of the build: a ticket goes in, a lane drafts the packet section and
its gates/tests from the scout's facts, and the runner turns that into a real `packet`
task — held as a draft until the operator lets it build.

## What landed

`src/inference_grid/board/plan_task.py` (new):

- `plan_task(board_dir, ticket, lane)` authors one board task of category `plan`. The
  ticket is `{title, body, repo, paths?}` (unknown keys refused): `repo` is the tree to
  scout and the tree the brief is written under, and `paths` names what the packet is
  about — absent, `guess_paths(repo, body)` takes the body's path-shaped tokens or greps
  its snake_case identifiers through git and returns the files that mention one (plain
  grep, deterministic, never a model). The brief `plan_brief(...)` carries a hard-rules
  section, the ticket, the scout's orientation section and the exact `packet.md` format
  the runner will parse; the task is the standard eleven keys plus `spec {base, paths}`,
  its only artifact `packet.md`.
- `validate_plan_task(raw)` reuses the shared task checks on a copy whose category
  `board/task.py` accepts (the provider-authored module is untouched, the same seam
  `packet`/`verify_merge`/`calibration_run` use) and owns the plan-specific `spec` shape
  (`base` a safe ref via `packet_task.BASE_REF`, optional `paths`).
- `parse_packet(text)` reads the lane's `packet.md`: the first `#### <id>. <title>`
  heading, then the first fenced JSON object that names `gates` (`tests` required with
  it, `artifacts` optional, any other key refused). A heading-less or block-less file,
  and a block whose JSON does not parse, raise with the reason.
- `settle_plan(task, board_dir, project_root, output_dir, lanes_view)` is the runner's
  code node: it cuts the hard rules out of the plan task's own brief
  (`lanes/brief.py::hard_rules`), builds the packet task (category `packet`, state
  `ready`, `spec.brief`/`packet_id`/`gates`/`base`/`max_rounds`, `tests` from the block,
  `artifacts` from the block or a `docs/reports/<id>.md` default, `lanes` from
  `route.default_lanes` for the packet's category), validates it through the same
  `validate_packet_task` a hand-written packet passes, writes the packet's brief and task
  file (existing files refused) and records the id in the board's drafts list. Everything
  is written only after the validation succeeds, so a bad block leaves nothing behind.
- The drafts list (`drafts.json`, excluded by name in `load_board`): a packet task the
  plan node authored is a draft, not a build. `board/runner.py::tick(..., auto_dispatch=)`
  reports `draft` and dispatches nothing for a listed task until the operator removes the
  id or the board config sets `"auto_dispatch": true`.

`src/inference_grid/board/runner.py`:

- `lane_view` carries the lane's `tier` (absent → `build`); J2 reads the same key in
  `route`, and J1 needs it to gate planning.
- A plan task is refused before selection when any lane it names is not `tier: plan`
  (`plan_requires_tier_plan: <lane>`), and is otherwise offered only the plan-tier lanes,
  with `plan` implicitly among their categories the way a canary is; plan tasks may run on
  an unverified or unqualified lane (no `plan` category rows exist to earn).
- A completed plan attempt goes through `settle_plan`: `passed` with a `plan` ledger
  outcome and the drafted id in the result row, or `blocked` with `plan: <validation
  error>` and its outcome recorded `False`. No review task is spawned for a draft.

`src/inference_grid/lanes/route.py`: `default_lanes(category, lanes)` is route's
defaulting rule factored out (every lane declaring the category, minus `explicit_only`
families); `route` calls it and the plan node uses it to give a drafted packet its build
lanes. Pure refactor — the route tests are unchanged and pass.

`src/inference_grid/cli.py`: `board-new --json {ticket, lane, board_dir}` routes to
`plan_task` (and `project_root` becomes optional, guarded, since the ticket carries the
repository); `board-tick` gains `auto_dispatch`, threaded through the `boards` config
passthrough beside `auto_land`.

`docs/BOARD.md`: the category list gains `plan`, and a **Plan node** section describes
the ticket path, the tier refusal, the settlement and the draft/`auto_dispatch` rule.

## Tests

`tests/test_plan_task.py` (10 cases, all offline). The lane process seam
(`worker.execute`) is faked so an attempt completes with a `packet.md` the test controls;
staging, admission, routing, settlement and the ledger run as production runs them.

- The scout section reaches the prompt: the staged `brief.txt` a flat lane reads carries
  the orientation (the named file, its commit subject, its pinning test) beside the
  ticket and the ask.
- A fake lane's `packet.md` becomes a valid packet task: the task file loads through
  `load_board`, its `spec` is exactly the block's gates plus brief/packet_id/base/rounds,
  its `lanes` are route's defaulting, its brief passes `hard_rules` and `packet_text` (so
  the packet node could dispatch it), it is listed in `drafts.json`, and the plan run
  records one accepted `plan` outcome.
- A malformed block holds the plan task: an unparseable fenced block blocks with
  `packet.md JSON block unreadable`; a readable block the packet validator refuses blocks
  with its own message (`each gate needs a name and argv`). Neither writes a packet task.
- Nothing builds without the flag: after the plan pass the second tick returns exactly
  `{"result": "draft"}`, a dry run reports `draft`, and with `auto_dispatch=True` the
  packet task is released to build (it then refuses for the fixture's non-packet lane kind,
  proving the gate opened).
- Planning is refused on a lane that is not `tier: plan`, including an absent tier.
- `guess_paths` takes a named path and, without one, greps the body's identifiers out of
  the tree.
- `validate_plan_task` refuses a foreign category, a missing `spec`, a bad base and
  unknown spec keys; `plan_task` refuses existing files; `cli.board_new` authors the plan
  task from a ticket and requires a lane.

## Defects found in existing code

None. Two judgment calls worth recording:

- **The draft mechanism.** The brief says the packet task is written `state: ready` and
  that "nothing is dispatched to build until the operator (or `auto_dispatch: true`) flips
  it". A ready task is dispatchable by definition, and the task schema (`board/task.py`,
  provider-authored) has no draft state, so the hold lives where a review's `source.json`
  already lives: a board-owned sidecar. `drafts.json` is excluded by name from
  `load_board`; the tick reports `draft` for a listed task and dispatches it only with
  `auto_dispatch`. An operator could instead flip the created task's `state` — the sidecar
  keeps the board honest either way, since the created file really is `state: ready`.
- **The plan brief's hard rules.** The created packet brief must open with a
  `## 1. Hard rules` section (the packet node cuts it there), and the plan lane is the
  one that knows the ticket's project. The plan node supplies a default hard-rules section
  and carries it across by code (`hard_rules(plan_brief)`), so the drafting lane never has
  to reproduce it verbatim; an operator with a project-specific umbrella brief edits the
  created brief. `ticket["paths"]` is also treated as authoritative when present — the
  grep runs only when the ticket names nothing.
- **`tier` on the config.** `lanes/config.py` is provider-authored and its fixed key set
  has no `tier`, so a `lanes.json` carrying one would be refused until J2 (whose brief
  owns the key) widens that set. J1 reads `tier` in `lane_view` with a `build` default, so
  it works the moment J2 lands; tests exercise it through the runner's `lanes` argument.

## Final gate

- Full suite before the change (`git stash` at `6277de4`, same interpreter and flags,
  `--continue-on-collection-errors`): the sorted `FAILED`/`ERROR` set is **89 entries**;
  after: the same **89**, `comm` empty in both directions. The 89 are the 88 pre-existing
  sandbox refusals (`worker.stop_group` cannot `killpg` an exited process group and
  `/bin/ps` is denied, failing every test that dispatches through the real worker) plus
  the `tests/test_calibration.py` deny-read collection error — both present at the base,
  which is why the new plan tests fake the worker seam rather than the lane argv.
- `ruff format --check` and `ruff check` on the six changed Python files: clean.
- No provider-authored file was touched (`board/task.py`, `lanes/select.py`,
  `lanes/config.py`, `lanes/sandbox.py`, `observation.py`, `goat_outcomes.py`,
  `remaining_units.py`, `provider_report.py`, `lane_readiness.py`, `flash_window.py`).
  The one non-J1 edit to a shared module is `route.default_lanes`, a behaviour-preserving
  extraction of code that already lived in `route`.
