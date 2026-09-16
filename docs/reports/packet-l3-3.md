# DeepSeek-V4.1-Flash lane report — packet L3: an idle agent round ends early with `agent_idle` (2026-09-16)

Lane: DeepSeek-V4.1-Flash (packet lane). Base: `53d5a2f` (the tip of `glm/work` at branch
time). Branch: `packet/packet-l3-3`, one commit, not pushed.

## Why

`lanes/packet.py::build_loop` ran every round until the adapter exited or the wall, so a
session that stopped working — a CLI stuck in a retry loop, an endpoint that answered once
and then hung, a model that burned a turn on nothing — held the attempt until
`wall_deadline` an hour later. The brief names the cost: J4 on cline-deepseek and the first
Q5 each lost an hour to exactly that, and the operator owed nothing for either. A round
that neither moves the tree nor writes to its transcript is not working; it should be cut,
the gates run as though it had exited, and the next round told plainly that the previous
one did nothing.

## What landed

**`src/inference_grid/lanes/packet.py`** — the watchdog inside the round wait; no new
module, no provider-authored file touched.

- **`tree_marker(work)`** — `rev-parse HEAD` plus `git status --porcelain`, the cheap pair
  the brief names. Deliberately weaker than `worktree_fingerprint` (it does not hash
  untracked file *contents*) because the watchdog reads it every few seconds. A directory
  that is not a git worktree, or a git command that fails, returns `None`, which is never
  read as "unchanged" — the same rule `worktree_fingerprint` already documents.
- **`IdleWatch`** — the two conditions the brief states. The marker and the transcript size
  are read at construction; `poll()` re-reads both and resets the window on either a moved
  tree or `IDLE_TRANSCRIPT_BYTES` (2 KB) of new transcript. It returns True only when the
  marker is readable, has stood still for the whole `idle_seconds`, and the transcript
  grew by fewer than 2 KB across that span.
- **`_wait_for_round(proc, deadline, clock, watch, poll, grace)`** — replaces the bare
  `proc.wait(timeout=remaining)`. It waits in steps bounded by `min(remaining,
  _poll_seconds(idle_seconds))` (`_poll_seconds` = `min(15, max(0.05, idle/4))`), returns
  `wall_deadline` on the wall, `process_exited` on a normal exit, and `agent_idle` when the
  watch fires — killing the process group and reaping it in the last two cases, exactly as
  the wall path always did.
- **The round's aftermath** — an idle round is counted; its gates run as usual; and the
  next round's prompt (`fix_prompt(..., idle_minutes=...)`) opens *"the previous round
  produced no change in 15 minutes; commit what you have or say why."* followed by the
  normal failed-gate tails. Two idle rounds in one attempt — or a single idle round with no
  round budget or session left to continue on — settle the verdict `reason: agent_idle`
  instead of burning the rest of the wall. A green gate still wins first, so an idle round
  that happened to leave the tree passing is `gates_passed`.
- **`idle_seconds`** — a `build_loop` keyword (`DEFAULT_IDLE_SECONDS = 900`); `None`/`0`
  disables the watchdog. A `kill_grace` seam (default 5 s, unchanged behaviour) lets the
  cut's reaping wait be shortened in tests. The verdict records `idle_seconds`.

**`src/inference_grid/board/packet_task.py`** — the spec key. `validate_packet_task` accepts
`spec.idle_seconds` (int 1..3600, bools refused) and fills the 900 default the way it
already fills `max_rounds`; `run_packet` passes it to `build_loop`. A held packet whose
verdict names `agent_idle` therefore reaches the operator as such —
`board/fix_packet.py` already lists `agent_idle` in `ELIGIBLE_REASONS` (M1 anticipated the
watchdog) and `docs/BOARD.md` already documents it, so no hold-path change was needed.

## Boundaries worth the operator's eye

- **The transcript the watch reads is the filtered one.** `stream_transcript` (e4) drops
  per-token delta lines as they arrive, so a round that spent the window streaming only
  `thinking_delta` events has a transcript that did not grow and can be read as idle even
  though the model was busy. The window is a task-spec knob precisely for a lane that
  thinks for long stretches without an event; the default 900 s is the brief's. The
  consequence of a false cut is bounded — the gates still run, the next round is told, and
  two idle rounds hold — but it is a real reading of "the transcript" and is named here
  rather than papered over.
- **Two idle rounds means two idle rounds, not two consecutive ones.** A round that makes
  progress and then idles twice still holds `agent_idle`; the round budget is what caps the
  attempt.
- **A non-git worktree never idles.** The tests' plain directories and any attempt whose
  clone git cannot read simply run to the wall, as before.

## Tests

`tests/test_packet_lane.py`, +4 (offline, real subprocesses, short windows):

- `test_two_sleeping_rounds_are_cut_at_the_idle_window_and_hold_agent_idle` — a fake CLI
  that prints its session id (flushed, or the pipe buffer would hide it) and sleeps is cut
  at a 0.4 s window well inside the 10 s wall; its gates run; two such rounds settle
  `reason: agent_idle` with both `agent_reason`s `agent_idle`, and round 2 re-enters the
  same session on a prompt whose first line is the idle note and which still carries the
  gate tail.
- `test_a_round_that_keeps_writing_is_not_cut_as_idle` — a fake CLI streaming 400-byte lines
  every 5 ms past the window is not cut (`agent_reason != agent_idle`) and leaves a
  transcript larger than 2 KB.
- `test_the_idle_prompt_names_the_window_and_asks_for_a_commit_or_a_reason` — `idle_minutes`
  (900 → 15, sub-minute → 1) and the prompt's first line.
- `test_the_idle_watch_needs_a_readable_tree_and_resets_on_activity` — a `FakeClock` drives
  the watch through a non-worktree (never idle), a transcript growth, and a tree move, each
  resetting the window, then a whole quiet window returning True.

`tests/test_packet_task.py`, +1 and 3 new refusal cases: the task spec's `idle_seconds`
reaches `build_loop` (a spy wrapping the real loop), the validator fills 900 by default and
refuses 0, `True` and 3601. `tests/test_plan_task.py`'s exact packet-spec expectation gains
the new key.

## Verification

- Full suite with the change and at the stashed base: **byte-identical failure sets** — 89
  inherited (`PermissionError` deny-reads of the operator's `~/.config/inference-grid` and
  the calibration corpus's module-level `skipif`, plus the sandbox `killpg`/`/bin/ps`
  denials), **zero new, zero repaired**. Passed 634.
- `ruff format --check` and `ruff check` clean on the five changed files. (Repo-wide ruff is
  not clean at the base: the deliberately minified `deployments/capacity/` collectors and
  the staged `grid/` review copies carry pre-existing `E701`/`E702`/`F401`.)
- No provider-authored file was touched. No new credential access; no absolute home path or
  personal e-mail address in the diff.

## Round-2 fix: the trailer's case (2026-09-16)

The round-2 `commit` gate failed on one line: `commit trailer missing: Co-Authored-By:
Deepseek-V4.1-Flash <noreply@deepseek.com>`. The packet's first commit signed the work
`Co-Authored-By: DeepSeek-V4.1-Flash <noreply@deepseek.com>` — capital `S`. The expected
string is built by `lanes/brief.py::trailer_for`, which capitalizes each hyphen-separated part
of the model id's last segment (`Deepseek`, `V4.1`, `Flash`) and nothing else, and the gate's
check is a case-sensitive substring test, so `DeepSeek-V4.1-Flash` does not contain
`Deepseek-V4.1-Flash`. The work itself is unchanged; only the signature's case was wrong.

The same gate (`lanes/brief.py::commit_gate_script`, the twin of `lanes/gates.py::run_commit`)
also requires **exactly one commit ahead of base** — `git log --oneline origin/glm/work..HEAD`
must be one line — which is the packet's own instruction ("Finish with exactly ONE commit on
this branch"). The count was already green; adding the round-2 commit the fix prompt asks for
would have failed the gate with `expected exactly one commit ahead of origin/glm/work, found
2`. Where the generic fix prompt ("a NEW commit, never amend") and the packet's commit gate
disagree, the gate is the check under test, so this fix rewrote that one commit's message
(message only, plus this section) instead of adding a second commit. HEAD is a new object and
the tree is clean; `git log --oneline origin/glm/work..HEAD` is still one line, and the gate
re-run prints `commit gate ok`.
