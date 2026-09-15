# Handoff brief 15 — the gates deserve the tests the packets get (any lane)

Follows `docs/handoff-glm-14.md`. **Section 1 of `docs/handoff-glm.md` applies unchanged.**
The commit trailer names the model that did the work (`Co-Authored-By: <model> <noreply@vendor>`;
the driver tells you which). Branch from `glm/work`. Do not touch files owned by a brief-14
packet still in flight: `src/inference_grid/watch.py`, `board/verify_merge.py`,
`board/policy.py`, `lanes/route.py`, `operator_queue.py`.

## Why this brief exists (2026-09-15, 01:40–03:00 UTC)

Brief 14 ran on three concurrent lanes through `scripts/run_lane.py`. Every packet failure in
that window was a **gate the coordinator wrote**, not the agent:

| Gate | What it did | Effect |
| --- | --- | --- |
| `ruff format --check .` | the repository is not format-clean (50 files) | both agents began a repo-wide reformat to get past it |
| `git grep /Users/` | matched its own source, the brief and a `/Users/example` fixture | every lane failed every round; one agent rewrote the gate as `'/Us'+'ers/'` |
| pytest with the shell's env | `ANTHROPIC_BASE_URL` leaked into `test_lane_zai` | one pre-existing failure failed the packet |
| `cline --id <session> <prompt>` | JSON mode refuses a prompt with `--id` | every Cline round after the first died in 20 s |

And around them: a `cmd` transcript reached 1.6 GB of `thinking_delta` lines on a disk at 93%;
GLM-in-Cline ended its turn after 9 iterations with a text reply and no commit, reported as
`completed`; a macOS upgrade rebooted the Mac and every hand-started process died while the one
`KeepAlive` job survived; `cline` had been SIGKILLed for a day (its postinstall breaks the
binary signature on macOS 27) before anyone ran `--version`; lane launches were 200-character
shell lines and zsh word-splitting broke two of them.

The packet loop did its job each time — refused to pass what the gates had not proved, and the
transcript said why. The lesson is that gates are code nodes and get tests like any other.

---

## Phase E — gates and the loop

#### E1. `lanes/gates.py`: the driver's gates as a tested module
Move `gates_for`, `commit_gate_script` and the scoped-ruff and home-path scripts out of
`scripts/run_lane.py` into `src/inference_grid/lanes/gates.py`, each gate a small function
returning a `packet.Gate`, the scripts as module-level Python (not string literals) invoked as
`python -m inference_grid.lanes.gates <gate> <args>`. The driver imports it; behaviour unchanged.
- Tests (`tests/test_gates.py`), each against a temp git repo: scoped ruff sees only changed and
  untracked `.py` files and passes when none changed; the home-path gate matches the runtime
  home only in changed files and ignores `/home/example`-style fixtures and its own source; the
  commit gate's three refusals (count, trailer, dirty tree) and its pass; the trailer is a
  parameter.
- Size: medium.

#### E2. Baseline-aware pytest gate
A packet must not fail for a failure it did not cause. `gates.pytest_gate(base)` runs the suite
once at `base` in a scratch worktree (cached per base commit under the attempt's parent
directory, so N packets on one base pay once), records the failing node ids, then runs the suite
on the branch and fails only on **new** failures — naming them — while reporting pre-existing
ones as `inherited: [...]` in the gate result. The brief's instruction to capture the baseline by
hand is deleted from `docs/handoff-glm.md`'s conventions; the harness does it.
- Tests: an inherited failure passes the gate and is named; a new failure fails it; a fixed
  inherited failure is reported as `repaired`; the cache is reused for a second packet on the
  same base.
- Size: medium.

#### E3. Early-stop detection in the packet loop
When an agent round exits 0 and (a) no gate result changed from the previous round and (b) the
worktree has no new diff since the round began, the round's `agent_reason` is
`agent_stopped_early` (not `process_exited`), the verdict's `reason` on exhaustion is
`agent_stopped_early` rather than `rounds_exhausted`, and the fix prompt for the next round
says so in its first line ("your previous session ended without changing anything"). Cline's
`finishReason` and Command Code's terminal event are read for corroboration and recorded.
- Tests (`tests/test_packet_lane.py`): fake adapter that writes nothing → early stop after
  round 1 with the named reason; an adapter that changes files but fails gates → the existing
  `rounds_exhausted` path unchanged.
- Size: small.

#### E4. Streaming transcript compaction and a disk guard
`packet.build_loop` writes the agent's stdout straight to `native-<n>.jsonl`. Route it through
a line filter that drops `DELTA_EVENTS` as they arrive (keeping a running count in
`native-<n>.compacted.json`), so the file never holds them; `compact_transcripts` stays for
old attempts. Before each round, refuse to start when free space under the packets root is
below a threshold (`min_free_bytes`, default 5 GB) with `reason: disk_low`, and record the
free-space figure in the verdict.
- Tests: a fake agent that streams 10 000 delta lines and 3 events leaves a 3-line file and the
  count; the disk guard with an injected `free_bytes`.
- Size: small–medium.

## Phase F — the operator's tools tell the truth

#### F1. `doctor` executes every lane binary
For each lane whose kind is a CLI, `doctor` runs `<executable> --version` with a 20 s timeout
(through the same seam the launchd check uses) and reports `available` with the version, or
`unavailable` with the reason: non-zero exit, signal (name it: `SIGKILL` is what macOS 27 does
to a modified-signature binary), timeout, or missing. HTTP lanes report `n/a`.
- Tests: fake executables for each outcome.
- `docs/LANES.md`: the Cline postinstall note — `bin/.cline` is rewritten by postinstall and
  loses its signature on macOS 27; `CLINE_BIN_PATH` pointing at the platform package's binary
  is the workaround the launcher honours first.
- Size: small.

#### F2. `deployments/local/install.py` renders every runtime as KeepAlive
The reboot on 2026-09-15 killed the dashboard server, the feed and the boards loop — hand-started
— while the `KeepAlive` scheduler survived. `install.py` renders plists for **all four**
runtimes (`capacity-loop`, `capacity-feed`, `capacity-web`, `tick-boards`) from one template,
`KeepAlive: true`, `RunAtLoad: true`, `ProcessType: Interactive`, never `StartInterval`
(document why in the module docstring: launchd parks interval spawns for a GUI agent while
the display is off — `pended nondemand spawn = interval`). It prints the bootstrap commands
and never runs `launchctl`.
- Tests: rendered plists parse with `plistlib`, carry no `StartInterval`, and name the paths
  the operator passed.
- Size: small.

#### F3. A job file for the driver
`scripts/run_lane.py --job lanes.json` replaces the flag soup:

```json
{"repo": ".", "brief": "docs/handoff-glm-15.md", "base": "glm/work", "python": ".venv/bin/python",
 "packets_root": "~/.grid-workspaces/packets", "database": "sqlite:///...",
 "lanes": [
   {"name": "goat-glm", "clone": "~/.grid-workspaces/ig-lane-a", "adapter": "command_code",
    "model": "z-ai/glm-5.3-flash", "packets": ["E1", "E2"]},
   {"name": "cline-glm", "clone": "~/.grid-workspaces/ig-lane-c", "adapter": "cline",
    "model": "z-ai/glm-5.3-flash", "key_file": "~/.config/inference-grid/cline-pass.key",
    "packets": ["F1"]}]}
```

One process launches every lane as a subprocess, writes `lane-<name>.log` beside the packets,
and prints a table when all are done. The existing flags keep working for one lane. This is the
file D1's board integration reads too.
- Tests: the job file validates (unknown keys refused, relative paths resolved against the
  file's directory); one lane per entry launched through an injected spawn.
- Size: small–medium.

#### F4. Two upstream reproductions, as documents
`docs/upstream/cline-postinstall-signature.md` and `docs/upstream/cline-id-json-prompt.md`:
what was observed (commands, exact error text, macOS and Cline versions), the minimal
reproduction, and the workaround. Ready to paste as issues. No code.
- Size: small.

---

## Definition of done, per packet
One commit with the trailer the driver names, one CONTRIBUTIONS row under `## 2026-09-15 —
brief 15`, `ruff format` and `ruff check` clean on the files you changed, the suite green except
inherited failures (the harness names them; you do not need to capture a baseline by hand once
E2 lands — until then, the driver's pytest gate is the judge). Report in
`docs/reports/brief-15-<packet>.md`.

## Phase G — the Cline subscription over HTTP (any lane)

#### G1. `cline-http`: the Go lane kind pointed at ClinePass
`lanes/go.py` hard-codes the OpenCode Go endpoint. ClinePass exposes the same OpenAI-compatible
chat-completions protocol at `https://api.cline.bot/api/v1/chat/completions`, with two
differences observed on 2026-09-15: the response body is wrapped as `{"data": {...}}`, and
only some models are covered by the subscription on the raw API (`z-ai/glm-5.3-flash`
answered; `moonshotai/kimi-k3` and `deepseek/deepseek-v4-flash-0731` returned
`402 insufficient_credits` — those are billed to pay-as-you-go credits unless called through
the Cline client). Make the endpoint a property of the lane's `provider` (`opencode` → the Go
URL, `clinepass` → the Cline URL) inside `go.py` — `lanes/config.py` is provider-authored;
do not add a key there — unwrap `data` when present, classify `402` as
`refusal: model_not_in_plan` (never retried), and record the `provider` field of the response
in the verdict.
- Tests: injected `send` returning the wrapped shape; the 402 refusal; the endpoint chosen
  per provider; existing Go tests unchanged.
- `docs/LANES.md`: a `cline-http` entry (provider `clinepass`, family `glm`, model
  `z-ai/glm-5.3-flash`, kind `go_http`, categories `independent_review`, `pure_function`, key
  path only) and the plan-coverage note above.
- Size: small–medium.

## Phase H — the GOAT and Cline accounts admit work

#### H1. `board_prepare.py` configures `goat-account` and `cline` from their observations
`docs/LANES.md` ("board-prepare", from brief 14 C1) describes it; `deployments/local/board_prepare.py`
does not do it yet, so the ledger's `goat-account` reading is from 2026-09-12 and `cline`
has no windows — neither lane can be admitted. Implement exactly what the paragraph says:
- `goat-observation.json` (`collect_goat.py`): windows `five_hour`, `weekly`, `monthly` as
  `used_percent`; caps 14 / 35 / 70 credits; remaining = cap × (100 − used) / 100;
  `configure_account("goat-account", 1, remaining, observed + 900, [<models the lanes name>],
  [<lane ids with provider goat>], observed_at=observed)`; a lane record per goat lane with
  `used_percent_max`, `admission_limit_percent` 80, `quota_freshness_seconds` 900.
- `cline-observation.json` (`collect_cline.py`): the same three windows as percentages with no
  published unit caps — use a 100-unit scale per window (remaining = 100 − used), `ok` only
  when all three windows answered; account id `cline`.
- Lane ids and models come from the config (`goat_lanes`, `cline_lanes`: lists of
  `{lane, model}`), never from `lanes.json`; absent config → those accounts are left alone.
- Also port the same into the operator's running copy? No — the operator cuts over to this
  file; say so in the report.
- Tests (`tests/test_local_board_prepare.py` or extend the existing): fixture observations →
  the exact `configure_account` and `record_lane` calls through a fake ledger; a stale or
  non-ok observation leaves the account untouched and records the lane `stale`.
- Size: small.
