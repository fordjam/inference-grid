# GLM lane report — brief 14, E3: early-stop detection in the packet loop (2026-09-15)

Lane: DeepSeek-V4.1-Flash via Command Code. Base: `origin/glm/work` at `829e95b` (the
branch point; `origin/glm/work` has since moved to `e71c1ca`, the E2 cache-dir threading).
Branch: `glm/e3-early-stop-detection-in-the-packet-loop`, one commit, not pushed.

The packet is E3 from `docs/handoff-glm-15.md`; the driver names this row and report
"brief 14", so they follow the driver's naming.

## What landed

`src/inference_grid/lanes/packet.py` — a round that exits 0 without moving anything is now
named, and the next session is told:

- **`worktree_fingerprint(work)`** (new) — a sha256 over `git rev-parse HEAD`, `git diff HEAD`,
  `git status --porcelain` and every untracked file's bytes (`git ls-files --others
  --exclude-standard -z`). It sees commits, unstaged and staged edits, and new untracked
  files with their contents. A directory that is not a git worktree, or a git command that
  fails, returns `None`: an unreadable worktree is **not** evidence the agent did nothing,
  so the caller never reads `None` as "unchanged".
- **`terminal_corroboration(native)`** (new) — the last terminal line of the round's native
  transcript, recorded for the record and never the decision. Cline's JSON mode ends with
  `{"type": "run_result", "finishReason": …}` → `{"kind": "cline", "finish_reason": …}`;
  `cmd --print --output-format json` ends with `{"type": "result", "subtype": …,
  "stopReason": …}` → `{"kind": "command_code", "subtype": …, "stop_reason": …}` (the shape
  `goat_outcomes.classify_goat` already reads). Unknown streams return `None`. It rides in
  each round as `agent_terminal`.
- **`build_loop`** captures the fingerprint before the agent run and again when it exits,
  and marks the round `agent_stopped_early` when **all** of: exit code 0, a clean
  `process_exited` (not a wall deadline), the two fingerprints equal, and the gate marks
  (`name`, `ok`, `reason`, `returncode`) equal to the previous round's. With no previous
  round there is nothing that can have changed, which is what lets a first round that does
  nothing be an early stop. A detected round does **not** end the loop: the failure path
  sets `reason` to `agent_stopped_early` instead of `rounds_exhausted` and continues, so the
  nudge reaches the session. If a later round's gates pass, the verdict is `gates_passed`;
  if the rounds run out, the verdict keeps `agent_stopped_early`.
- **`fix_prompt(..., stopped_early=False)`** — when the round being answered was an early
  stop, the prompt's **first line** is `your previous session ended without changing
  anything: … Make the change now, in this session, and leave it on the branch.` The rest of
  the prompt (round counters, failed gates, tails, the NEW-commit/trailer/do-not-weaken
  rules) is unchanged.

## Tests

`tests/test_packet_lane.py` (+4, all offline, real temp git repos for the fingerprint):

- `test_a_round_that_changes_nothing_is_an_early_stop` — git worktree, an adapter that exits
  0 writing nothing; `agent_reason == "agent_stopped_early"`, verdict `reason ==
  "agent_stopped_early"` after one round, the emitted Cline terminal recorded as
  `agent_terminal == {"kind": "cline", "finish_reason": "completed"}`, and no verification
  claimed.
- `test_a_round_that_changes_files_but_fails_gates_exhausts_rounds` — git worktree, an
  adapter that writes a fresh untracked file and a failing marker every round: `reason ==
  "rounds_exhausted"`, both rounds `process_exited`, two agent calls. This is the packet's
  "changes files but fails gates → the existing path unchanged".
- `test_the_early_stop_prompt_tells_the_next_round_nothing_changed` — after an early stop the
  loop resumes the same session with a prompt whose first line starts `your previous session
  ended without changing anything`, and the prompt is what `prompt-2.txt` holds.
- `test_terminal_corroboration_reads_both_clis_terminal_events` — the Cline `run_result`
  shape, the Command Code `result` shape, a non-JSON file and a missing file.

## Defects found in existing code

None in the loop's logic. One pre-existing fixture encoded the old behaviour and had to
change: `tests/test_packet_task.py`'s `FAKE_AGENT` rewrote an identical report on a
re-entry round (and, being committed already, staged nothing), so the second round moved
nothing and the board test `test_a_failing_gate_holds_the_attempt_and_leaves_the_branch`
read `rounds_exhausted`. Under E3 that round is exactly an early stop. The fake agent now
appends one more report line per round, so a re-entry round genuinely changes the worktree
and commits — the fixture becomes the "changes files but fails gates" case the packet names,
and every assertion in that test, including `packet loop rounds_exhausted`, is left
byte-identical. No assertion was relaxed, skipped or deleted.

## Final gate

- `pytest -q`: **88 failed, 415 passed, 7 skipped** (+4 new tests). The baseline-aware gate
  was run directly (`python -m inference_grid.lanes.gates pytest origin/glm/work <cache>`):
  **88 inherited, 0 repaired, no new failures, exit 0**. The 88 are the pre-existing
  sandbox `killpg`/`/bin/ps` `PermissionError` failures, byte-identical to the base.
- `ruff format --check` and `ruff check` on `src/inference_grid/lanes/packet.py`,
  `tests/test_packet_lane.py` and `tests/test_packet_task.py`: clean.
- `git log origin/glm/work..HEAD --oneline` names exactly one commit, with the DeepSeek
  trailer.
