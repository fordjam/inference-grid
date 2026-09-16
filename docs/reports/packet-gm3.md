# GLM-5.3-Flash lane report — packet M3: the autonomy policy, written and enforced (2026-09-16)

Lane: GLM-5.3-Flash (packet lane). Base: `3ae92a3` (the tip of `glm/work` at branch time).
Branch: `packet/packet-gm3`, one commit, not pushed.

## Why

The grid has been running on folklore. Every lane report and board module carries a piece
of the line — admission limits live in the ledger, the requeue rule in the runner, the
draft-then-release rule in the plan node, landing behind `auto_land` — but nothing said,
in one place, what the grid may decide by itself and what always waits for a person. A
new lane (or a new operator) reconstructs the policy from code archaeology, and nothing
fails when the written line and the enforced one drift apart. This packet writes the
policy down and pins the writing to the code with a test, and adds the one wait that
needed a mechanical gate: a board's `owner_only` path prefixes.

## What landed

**`docs/AUTONOMY.md`** (new) — the policy: a table of what the grid does alone
(dispatch ready tasks within each account's admission limit; requeue a dead attempt;
draft a fix packet; land a packet whose gates and independent review passed, on a board
with `auto_land`; publish observations) and what waits (anything that changes published
research numbers, the holdout register, credentials, provider configuration, deploys,
spend beyond the configured limit, and a packet whose files fall under a board's
`owner_only` prefixes). The prose maps each row to where it lives, and is explicit that
the waits are enforced mostly by absence — no code path writes research numbers, the
holdout register, credentials, provider configuration or a deploy; the spend wait is the
ledger's admission refusal — and that `owner_only` is the one wait with a dispatch-time
check. A last paragraph says the table and `board/policy.py`'s constants change
together, and names the test that holds them to it.

**`src/inference_grid/board/policy.py`** — the executable half, beside the review
waiver the module already held (the module docstring now introduces both halves):

- `ALONE` and `WAITS` — the two tuples the document's table mirrors.
- `owner_only_prefix(task, prefixes)` — the first prefix one of the packet's declared
  files falls under. Matching is directory-style (`docs/research` covers
  `docs/research/x.md` and itself, never `docs/research-notes.md`; a trailing slash
  names the same directory). The declared files — inputs (the brief among them), tests,
  artifacts — are the whole of what a packet can touch: staging copies exactly them into
  the attempt. Entries that are not non-empty strings are ignored, so a half-written
  key narrows the gate instead of crashing the tick.

**The `owner_only` board-config key** (list of project-relative path prefixes, default
`[]`), threaded end to end:

- `cli.py::board_tick` gains the named parameter and the multi-board branch's key
  filter, so a tick-all board config carries it like `auto_land` does.
- `board/runner.py::tick` refuses an owner-only packet before routing, where the draft
  gate sits: no lane is selected, no attempt is opened, nothing is spent, the task stays
  `ready`, and the tick row names the prefix that matched — `owner_only: <prefix>`. A
  dry run plans the same refusal, so `--dry-run` output says why the packet will not
  move. A packet outside every prefix dispatches exactly as before (test-pinned).
- `digest.py` lists ready owner-only packets under *Needs you* —
  ``- owner-only: `<task>` — a declared file is under prefix `<prefix>` (board <name>)``
  — ahead of the eval-coverage rows, with the trailing `none` line suppressed when the
  section has content. The operator sees the reason a packet never moves where they
  already look each morning.

Deliberate readings of the brief's letter, disclosed: the gate covers a packet's
*declared* files (the brief names "brief section touches files" — staging makes the
declared lists exactly that), and it applies to packets only, the only kind whose
settlement becomes a landing. Drafts are untouched by the placement: an unreleased draft
is already operator-held, and with `auto_dispatch` a drafted packet reaches the gate and
is refused like any other. The needs-you listing reads the board config directly, so a
packet is listed whether or not a tick has run since the key was added.

## Tests

- **`tests/test_autonomy.py`** (new, offline): the document's table mirrors `ALONE` and
  `WAITS` in order — the brief's "a test reads both" — plus a population guard (a
  silently emptied table would mirror a silently emptied tuple), and the matcher:
  directory-style prefixes, the file itself, a trailing slash, a sibling directory that
  shares the string, the empty and absent key, and junk entries.
- **`tests/test_packet_task.py`**, +3 (offline): an owner-only packet is refused with
  the prefix — row `owner_only: docs/reports`, lane and attempt `None`, task `ready`,
  the ledger attempt list on the account empty; the refusal is planned dry; and a
  packet outside every prefix still dispatches to `passed` (the gate is the prefix, not
  the flag).
- **`tests/test_digest.py`**, +1 (offline): a board config with `owner_only` and one
  matching ready packet task produces exactly one needs-you row naming the task and the
  prefix; a non-matching ready task produces none.

## Verification

- Full suite (`--ignore=tests/test_calibration.py`, which cannot collect under this
  environment's deny-read policy — at the base too): 88 failed / 649 passed — the
  failing set byte-identical to the `3ae92a3` base (verified by diffing the sorted
  `FAILED` lines of full runs at the base, in a scratch worktree, and on this branch:
  88 inherited sandbox denials, zero new, zero repaired; the +7 pass delta is this
  packet's seven tests).
- The scoped ruff gates (`python -m inference_grid.lanes.gates ruff <base> format|check`
  and the same invocations over all seven changed files): clean both ways. One real
  finding fixed (`F401`, an unused import in the new digest test).
- The repo-wide `ruff format` sweep reflowed 34 files this tree does not own — the
  installed ruff's style has moved since the tree was formatted, and the harness's own
  ruff gate is deliberately scoped to the packet's files for exactly this reason
  (`gates.py::run_scoped_ruff`: "the repository is not format-clean"). All 34 were
  reverted; the seven files this packet owns are format-clean, and no
  provider-authored file (`observation.py`, `goat_outcomes.py`, `remaining_units.py`,
  `provider_report.py`, `lane_readiness.py`, `flash_window.py`, `lanes/config.py`,
  `lanes/select.py`, `lanes/sandbox.py`, `board/task.py`) is touched by the diff.
- No new credential access, no absolute home paths, no network in any test.
