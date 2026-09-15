# GLM lane report — brief 14, packet A1: `watch` (2026-09-15)

Lane: glm (glm-5.3-flash via Command Code), build lane B. The packet was built on
`glm/work` at `8302bf9`; the harness's gate rounds and the coordinator's own progress
(Phase D, A2's landing, brief 15, the run_lane --resume work) moved the base twice
since, and the branch now carries exactly ONE commit on top of `origin/glm/work` at
`0c1b599`, rebased and squashed to satisfy the commit gate (see the last section).
Not pushed.

## What landed

- `src/inference_grid/watch.py` — the stall and staleness alarms as a code node. One pass
  reads only paths the spec names (the module never assumes a home directory and never
  reads a credential) and produces five alarm kinds, each `{"kind", "key", "since",
  "detail"}`:
  - `reading_stale` — per overlay account: `observed_at` older than `reading_stale_s`
    (an unparseable or absent timestamp counts as stale) or `status != ok` with the
    offending status (`auth_required`, `rate_limited`, `unknown`, `error`, …) named in
    `detail`. An unreadable overlay is an entry in the output's `errors` list, not a
    per-account guess.
  - `attempt_held` — every ledger attempt in the ACTIVE `held` state older than
    `held_s`, keyed by attempt id, carrying `task` and the hold `reason`.
  - `board_stalled` — a board with ≥ 1 `ready` task whose tick log's last
    `no_dispatch_passes` consecutive passes for that board contain no `passed`, `held`,
    `refused` or `rejected` result (substring match, so `refused: quota stale` and
    `review_rejected` count as productive). `since` is the logged time of the first
    pass of the stalled streak; `detail` carries the ready count. Unparseable log lines
    are ignored rather than guessed at; the log's JSON shape is `tick_all`'s
    (`{"time", "board", "seconds", "results": [{"task", "lane", "attempt", "result"}]}`,
    matched on `board == board_dir`).
  - `upload_stale` — `upload-status.json` missing (the stalest case), older than
    `upload_stale_s`, or carrying `status != ok`.
  - `cloud_behind` — the health endpoint returns JSON with a `commit` that differs from
    `git -C deploy_dir log -1 --format=%H -- .` (exactly the packet's command; newest
    commit touching the deploy bundle). `cloud_health_unversioned` is the bucket for
    "the cloud's build could not be confirmed": the old plain-text `ok` body, a JSON body
    without a usable commit, or an endpoint that cannot be reached. A `deploy_dir` whose
    commit cannot be resolved is an error entry, never a false `cloud_behind`.
- Edge triggering against `spec["state"]`: raised once when an alarm first appears,
  silent while unchanged, cleared once when it goes, re-raised after `renotify_s`
  (default 4 h) while it persists, with the original `since` preserved so the notifier
  names the whole duration of the problem. The state file is written only when the delta
  is non-empty. The delta entries are `{"event": "raised"|"renotified"|"cleared", …}`.
- Notification: the CLI prints `{"errors": [...], "delta": [...]}` as JSON, and for each
  script in `spec["notify"]` runs it with exactly that text on stdin (30 s timeout, one
  notifier failing recorded in `errors` without stopping the others). No Telegram client
  anywhere in the package — the notifier is the operator's script.
- CLI: `watch` added to `cli.py`'s choices and to the requires-`--json` set; the spec is
  the `--json` payload and carries its own `database` url.
- `deployments/capacity/cloud_server.py::/healthz` — now `{"ok": true, "commit":
  RAILWAY_GIT_COMMIT_SHA or GRID_DEPLOY_COMMIT or null}` with `Content-Type:
  application/json` (the watcher compares this against the deploy bundle's commit).
- Tests, all offline:
  - `tests/test_watch.py` — 14 tests. Every alarm kind from fixture files and a temp
    sqlite ledger (held/fresh-held/completed attempts); `board_stalled` needs both ready
    tasks and the full streak, resets on each of the four productive tokens, ignores
    boards without ready tasks; `upload_stale` missing/old/not-ok/fresh-ok;
    `cloud_behind` equal/unequal commits, unversioned for plain text, commit-less JSON
    and `URLError`; the default git resolver's argv asserted via a patched
    `subprocess.run`; edge triggering raise → silent → clear → raise → silent →
    renotify against a real state file with an injected clock; the notify hook receives
    exactly the printed delta (injected hook, and through `cli.main()` with the hook
    patched); a failing notifier is recorded and the next one still runs. `now`,
    `fetch`, `deploy_commit` and `notify` are function seams on `watch()` — no test
    opens a socket and no test executes a process.
  - `deployments/capacity/test_cloud.py::test_healthz_reports_the_deploy_commit_as_json`
    — status 200, `application/json`, `ok` true, `commit` null with no env, then
    `GRID_DEPLOY_COMMIT`, then `RAILWAY_GIT_COMMIT_SHA` taking precedence. No existing
    test asserted on the old plain-text `ok` body (nothing to update, nothing weakened).
- `docs/CONTRIBUTIONS.md`: one row under `## 2026-09-15 — brief 14`.

## What I could not verify here

- Real process execution is blocked in this lane's sandbox (`PermissionError` on exec,
  the same environmental fact behind the 80 pre-existing `killpg` failures), so
  `run_notify`'s subprocess path and `git_deploy_commit`'s real git call were verified
  only through a patched `subprocess.run` (argv and stdin asserted), never end to end.
  Both are four-line standard-library calls; the operator's first `watch` run will
  confirm them.
- `cloud_behind` against the deployed dashboard needs the `railway up` redeploy of
  `deployments/capacity` (operator step, as for every cloud change) before the endpoint
  answers JSON; until then the watcher reports `cloud_health_unversioned` for it, which
  is the intended behaviour for the current server.
- Operator step from the packet, not done here: write `notify.sh` piping stdin to the
  Telegram bot the desktop plugin uses, and add `watch` to A2's scheduler loop at a 60 s
  cadence.

## Defects found in existing code

`lanes/zai.py::build_env` stripped provider credentials from the inherited environment
but not `ANTHROPIC_BASE_URL`, so an operator shell that exports one leaks it into a
first-party Max-login run — contradicting `build_env`'s docstring ("the endpoint
variables stay unset") and the first-party test's "no endpoint override" assertion.
This lane fixed it in its round-2 repair commit (`ANTHROPIC_BASE_URL` joined
`STRIPPED_ENV`); the coordinator has since landed the same fix — together with the
hardcoded `DEFAULT_CLAUDE` home path this report also flagged — directly on
`glm/work`, so the branch carries no zai.py diff and the defect is fixed upstream.
Everything else behaved as its code and docs say. Two notes, neither a defect:
(1) the packet said "the existing tests that expect `ok` are updated" — no existing
test touched `/healthz`; the new one pins the JSON contract instead. (2)
`deployments/capacity/` had never been ruff-clean (see below); per the packet's own
gate that style is now gone in the two files this packet touched.

## Gate repairs (harness rounds 2–3)

Each harness-reported gate failure and its disposition. The base advanced twice during
the rounds (A2's landing, brief 15, the run_lane --resume work), and the coordinator
absorbed several round-2 recommendations directly onto `glm/work` — the scoped ruff
gates (`6b4f151`), the runtime home-path gate (`84c906d`, `abcf310`), the zai.py fixes,
the brief and sandbox-fixture rewords — so the final branch re-applies only what is
still this packet's own work, as one squashed commit: the commit gate demands exactly
one commit ahead of `origin/glm/work`, which outranks the "new commit, never amend"
instruction once the base has moved under a two-commit branch.

- **ruff-format / ruff-check** — the round-2 report argued these gates were repo-wide
  and unfixable (49 files / 196 errors); that was wrong for the gate that actually
  runs: `6b4f151` scoped both ruff gates to the Python files the branch changes against
  `origin/glm/work`. In scope here: `watch.py`, `cli.py`, `test_watch.py` (already
  clean) and the two dense capacity files this packet had touched. Fixed by running
  `ruff format` over `deployments/capacity/test_cloud.py` and `cloud_server.py` (the
  dense one-statement-per-line style is gone in these two files), dropping four
  genuinely unused imports (`base64`, `math`, `datetime.timezone` in the server; `time`
  in the test), splitting the test's one-line import, and rewriting the one lambda
  assignment in `_expire` as a nested `def`. No test was weakened: all 20 capacity
  tests pass from their own directory, including the `/healthz` contract test. The rest
  of the repo's dense style (`refresh_agent.py`, `upload.py`, …) is out of the gate's
  scope and untouched.
- **no-home-paths** — round 2 left this red as "the gate matches its own pattern
  literal and the brief that quotes it; only the coordinator can untangle that". The
  coordinator untangled it: the gate now reads the operator's real home directory at
  runtime and checks only the files the packet changed (`84c906d`, `abcf310`), the
  brief line no longer quotes the pattern, and the sandbox-fixture strings and
  `zai.py`'s `DEFAULT_CLAUDE` literal are gone from the base. Nothing left for this
  packet to do; the gate passes on the rebased branch.
- **pytest** — full suite after every change: **80 failed, 402 passed, 7 skipped**, and
  the same suite at the base commit (`origin/glm/work`, run in a second worktree) gives
  **80 failed, 388 passed, 7 skipped** — the 80 failure IDs are byte-identical between
  the two runs (the environmental `PermissionError`-on-exec class), and the 14 passing
  tests this packet adds are exactly the new watch suite. The base's own advances (A2's
  39 collector tests, brief 15) sit in the passing set. The round-2 real defect
  (`ANTHROPIC_BASE_URL` leaking into first-party runs) is fixed upstream (see above).
- **commit** — one commit, trailer `Co-Authored-By: GLM-5.3-Flash <noreply@z.ai>`
  present, tree clean, exactly one commit ahead of `origin/glm/work`.

Residual risk for the harness's pytest gate: it runs with `-x`, so the first
environmental exec failure stops the run before later tests. Every failure in this
checkout is that environmental class.
