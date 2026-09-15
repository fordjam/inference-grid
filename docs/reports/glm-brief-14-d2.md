# Report — brief 14 D2, `opencode_cli` lane kind (interactive ZCode, GLM-5.3-Flash)

Branch `glm/d2-opencode-cli-lane` from `main` (bac6d2e — main had advanced seven commits
past the 5479de4 named in the brief; `lanes/goat.py`, `lanes/sandbox.py` and the test
conventions I mirror are unchanged between the two). One commit. Not pushed; `glm/work`
and `main` untouched. Nothing under the Phase A–C file list was created or edited.

## What landed

- **`lanes/opencode.py`** — the lane, same contract as `lanes/goat.py`:
  - Workspace write sandbox scoped to the attempt's `work/` (`sandbox-exec` profile,
    `probe` before trust), the real home readable and **never writable** — the CLI keeps
    its login there (`~/.local/share/opencode/auth.json`).
  - Wall deadline: `proc.wait(timeout=lane["wall_seconds"])`, SIGTERM to the process
    group, then SIGKILL; the supervisor result (`process_exited`/`wall_deadline`) is part
    of the verdict.
  - Native evidence: the CLI's NDJSON event stream on stdout (`native.jsonl`), every
    line parsed; one malformed line refuses the run.
  - `opencode_outcomes.classify_opencode(rows, supervisor, model)` — a new module, mine:
    exactly one terminal `step_finish` event must echo the asked `modelID`
    (case-insensitive), the last assistant `text` part is the terminal text, and the
    terminal event's `tokens` ride the verdict as usage. Outcomes:
    `native_complete`, `interrupted` (wall deadline), `malformed_stream`,
    `missing_or_multiple_terminals`, `model_unqualified`, `empty_terminal_text`.
  - Receipt: goat-shaped (`status`/`finish_reason`/`actual_model`/`manifest_sha256`/
    `artifacts` with sha256 digests); reply-only tasks publish the terminal text as
    `reply.txt`.
  - `user_config_digests(home)` over the CLI's **config** (`~/.config/opencode/
    opencode.json`) **and auth** (`~/.local/share/opencode/auth.json`) files before and
    after the run; any change refuses with `user configuration changed`, even when the
    run looked native-complete.
- **Policy gate, decided by the operator, not the code.** `credential_denied(home,
  deny_roots)` checks the opencode auth path against the deny-read list before anything
  runs: when a deny root covers the auth file, the lane refuses to start with verdict
  `credential_denied_by_policy: …` — no sandbox probe, no worktree, no digest, no
  spawned process (the test asserts `native.jsonl` never appears). With the denial
  absent, the run proceeds with the full digest discipline.
  **What removing the deny entry exposes, plainly:** the sandbox bounds *writes*, not
  reads. With the entry gone, the model's own shell can read the auth key — into its
  context, a transcript, or any command output — and the before/after digest check
  still passes, because it proves only that the key was **not modified**, never that it
  was **not read**. The digest discipline is what makes allowing the read survivable
  (a tampered login is still caught); it is not a substitute for the decision. The same
  paragraph is in `docs/LANES.md`'s lane entry so it outlives this report.
- **Kind wiring without touching `lanes/config.py`** (provider-authored): the accepted
  kind set is *consumed* in `lanes/runner.py::KINDS`, which now maps
  `"opencode_cli": "opencode"`. The validation set inside `lanes/config.py` is the one
  thing this packet cannot grow, so a real `lanes.json` naming the kind will be refused
  by `validate_lane_config` until the coordinator applies this one-line patch:

```diff
--- a/src/inference_grid/lanes/config.py
+++ b/src/inference_grid/lanes/config.py
@@ -15,1 +15,1 @@
-    kinds = {"go_http", "goat_cli", "cline_cli", "claude_headless", "zcode_cli", "codex_cli"}
+    kinds = {"go_http", "goat_cli", "cline_cli", "claude_headless", "zcode_cli", "codex_cli", "opencode_cli"}
```

  (Same shape as the flagged `codex_cli` coordinator edit recorded in the module header
  and CONTRIBUTIONS handoff-13 L3.)
- **`docs/LANES.md`** — the packaged-kinds table row and the `go-agent` lane entry
  (documentation only: kind `opencode_cli`, family `glm`, model
  `opencode/glm-5.3-flash`, categories `pure_function`, `tests_multi_file`, no
  credential values), including the deny-read policy paragraph.

## Verification

- Baseline captured before any change on the D2 base (bac6d2e): **429 passed,
  7 skipped, 0 failed** — failing-test list empty. After: **439 passed, 7 skipped**
  (10 new tests in `tests/test_lane_opencode.py`), failing list byte-identical (empty).
  Two existing tests were updated for the fact that a seventh lane module is now
  packaged: `test_lane_defaults.py::test_doctor_names_packaged_modules_without_records`
  (the doctor's packaged-module scan now names `opencode` too) and
  `test_doctor.py::test_passing_checks_do_not_claim_provider_health` (registers every
  packaged module, now including `opencode`, for the passing report). Both are the
  doctor behaving as handoff-13 L2 specified — a packaged module without a ledger record
  is named until the operator registers it.
- The fake CLI covers happy path, file artifacts with digests, model mismatch, missing
  terminal, malformed stream, auth tamper through a symlinked watched file, wall
  deadline, denied-auth refusal before spawn, unrelated deny root not blocking, and
  `OpencodeAdapter.session_id` against captured event lines. The sandbox tests run for
  real here (`sandbox-exec` present). Tests pass `deny_roots` explicitly, so nothing in
  the suite reads the operator's real `deny-read.json`.
- Lint: all three new files are `ruff format`-clean and add no `ruff check` findings;
  the format-drift count over `src tests scripts` is unchanged from the base (35 before,
  35 after — the drift is pre-existing; see the D1 report for the same note with
  numbers), and `ruff check` reports exactly the one pre-existing finding `main` has.

## Not verified, and assumptions the operator should re-check

- **The classifier's event shapes are documented assumptions.** The repo's only
  captured opencode event line is the `step_start`/`sessionID` one reused by
  `OpencodeAdapter` (test_packet_lane). I assumed `{"type": "text", "text": …}` parts
  and a terminal `{"type": "step_finish", "reason", "modelID", "tokens"}`. Before the
  first real dispatch, capture one real `opencode run --format json` stream and check
  `opencode_outcomes.py`'s docstring against it — the module is structured so only the
  shape constants in one place need to move.
- **Session persistence under the read-only home.** The lane grants no writable root
  outside the worktree, so `opencode`'s own state directory cannot be written during a
  run. The one-shot lane contract does not need resume (that is the packet adapter's
  job, outside the sandbox), but if the CLI hard-fails on read-only state, the fix is a
  dedicated `extra_write_roots` entry for the state dir — never for the auth file.
- `DO_NOT_TRACK=1` is set for the run; whether the installed CLI honours it is
  unverified.
