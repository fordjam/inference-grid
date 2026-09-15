# Lane reference

Documentation only: the private `lanes.json` stays the operator's (validated by
`inference_grid.lanes.config`). Credential paths below are placeholders — the real paths
live only in the operator's `lanes.json`, mode 0600. Every lane earns its rows the same
way: `inference-grid lane-init --json {"lane_id": …, "board_dir": …}`, then one tick.

## Model tiers

Every lane record carries `tier: plan | build | review` — the operator's written-down statement of
what a lane is for. The policy is one sentence: SOTA models for plan and review, workhorses for
build. Planning a packet and reviewing one are judgement calls on a whole document, so they get the
lanes the operator has marked for thinking; building is bounded work against a packet, so it gets
the workhorses, and the expensive lane stays out of mechanical work. `route` reads the key —
`lanes/config.py` is provider-authored, so the read lives in `lanes/route.py` — and asks for the
tier the task's category implies: `plan` → plan, `independent_review` → review, everything else
(`packet`, `pure_function`, the multi-file categories, `canary`) → build. Among the lanes that fit
the packet, a lane declaring another tier is not offered and appears in the plan's `dropped` rows as
`tier_mismatch`, naming the tier it declares, so `board-tick --dry-run` says why a SOTA lane sat
out. With no lane of the asked tier offered, every fitting lane stays in play — which is how a
board whose records predate the key routes as it always did — and an absent key is `build`.

The key is what a lane view carries; the packaged `lanes.json` key set cannot hold it yet
(`lanes/config.py` accepts exactly its own field list), so the operator cannot declare a tier until
that set grows by one entry — the one-line coordinator edit, patch in the J2 report. The runner's
`lane_view` fills `build` for any lane that does not declare one, so a plan task has no lane to run
on in the meantime (`plan_requires_tier_plan`).

```json
"go":        { "model": "glm-5.3-flash", "tier": "build" },
"go-kimi":   { "model": "kimi-k3",       "tier": "review" },
"plan-lane": { "model": "glm-5.3-flash", "tier": "plan" }
```

## Packaged kinds

| Kind | Module | Family | Contract |
| --- | --- | --- | --- |
| `go_http` | `go` | varies by model | One chat-completions call to the lane provider's endpoint (`opencode` → the OpenCode Go subscription, `clinepass` → ClinePass); `reasoning_effort` per `REASONING_EFFORT`, `max_tokens` capped by the task's thinking budget |
| `goat_cli` | `goat` | glm | Command Code CLI, `--mod` session effort, config-hash guard, `classify_goat` |
| `cline_cli` | `cline` | qwen | Write sandbox, `iteration_end` snapshots with deletion restore, `classify_cline` |
| `claude_headless` | `zai` | claude | Anthropic-compatible base URL, `MAX_THINKING_TOKENS` from the task budget |
| `zcode_cli` | `zcode` | glm | Bundled ZCode CLI, session-DB evidence |
| `codex_cli` | `codex` | openai | Codex CLI headless (`codex exec`); `classify_codex` — see below |
| `opencode_cli` | `opencode` | glm | `opencode run --format json` as an agent with tools; event-stream evidence, config/auth digest guard, `classify_opencode` — see below. `lanes.json` cannot name the kind until `lanes/config.py`'s accepted set grows by one entry (provider-authored; patch in the D2 report) |

## Command Code GOAT

Documentation only: the fields below are what a `goat` entry declares; no credential
values are recorded here — the operator's `lanes.json` carries the credential path.

```json
"goat": {
  "provider": "goat", "family": "glm", "model": "glm-5.3-flash",
  "kind": "goat_cli", "executable": "/path/from/operator/commandcode",
  "categories": ["tests_multi_file", "fixtures_multi_file"]
}
```

Reserve for multi-file work; every call carries ~16–25k input tokens of fixed CLI
overhead, so small jobs are wasted on it.

## Cline

```json
"cline": {
  "provider": "cline", "family": "kimi", "model": "cline-pass/kimi-k3",
  "kind": "cline_cli", "executable": "/path/from/operator/cline",
  "categories": ["pure_function", "independent_review"]
}
```

Run only after the weekly reset (the account exhausts quickly); never give Cline size
hints — a passing artifact was once deleted to chase a line count. The ledger seeds a
`cline` alias placeholder on `init`; `configure_account` fills it in.

The subscription's models live in their own id namespace: `cline-pass/<model>`
(`cline-pass/kimi-k3`, `cline-pass/deepseek-v4.1-flash`, `cline-pass/qwen3.8-max`, …), listed
under "Subscribed" by `cline auth`. A vendor id (`moonshotai/kimi-k3`) reaches the same model
through pay-as-you-go credits and refuses with `insufficient_credits` when the balance is
empty, whether called through the CLI or the HTTP API — the plan windows stay at 0% and the
call never happens. Only the `cline-pass/` ids draw on the pass. The free tier
(`z-ai/glm-5.3-flash` and a few others, separate from the pass quota) is what `cline-http`
uses, with its own daily cap.

macOS 27 note: the package's `bin/.cline` launcher is rewritten by Cline's own
postinstall, which breaks its code signature — the kernel then SIGKILLs the binary and
every launch dies before it prints anything (`--version` names the signal). Point
`CLINE_BIN_PATH` at the platform package's real binary instead of `bin/.cline`: the
launcher honours that variable first, and `doctor` runs `--version` so a broken launcher
is a named `lane_binary_unavailable` finding rather than a silent failure.

## board-prepare

The capacity loop's `board_prepare.py` reconfigures each account from its observation
file before every board tick. The `goat` account is configured from
`goat-observation.json` (written by `collect_goat.py`: the `windowLimits` five-hour and
weekly readings plus the monthly credits read against the plan's 70-cap monthly window)
and the `cline` account from `cline-observation.json` (written by `collect_cline.py`:
the three usage-limit windows, `ok` only when all answered). Each observation's windows
map to remaining units against the documented caps — remaining share of the window ×
the window's unit cap, the same shape the zai and go accounts already use — and the
result, with the observation timestamp, is what `configure_account` writes for the
lane's admission decision.

## Codex (OpenAI) — `codex_cli`, pending operator confirmation

```json
"codex": {
  "provider": "codex", "family": "openai", "model": "<operator confirms the model id>",
  "kind": "codex_cli", "credential_path": "/path/from/operator/auth.json",
  "executable": "/path/from/operator/codex", "plan_units": {"five_hour": 1},
  "window": null, "max_concurrency": 1, "wall_seconds": 900,
  "categories": ["pure_function", "independent_review"]
}
```

The module runs the CLI headless (`codex exec` with `--json`/event output where the
installed CLI supports it — the operator confirms the exact flags for their version) and
classifies the event stream with `classify_codex`. Family `openai`; reviews are
explicit-only (the runner never picks `claude`/`openai` lanes unless the task names
them), which is what lets first-party T1 repositories run under the family rules.

## First-party Claude — `claude_headless` on the operator's Max login

```json
"claude": {
  "provider": "claude", "family": "claude", "model": "<operator confirms, e.g. claude-opus-4>",
  "kind": "claude_headless", "credential_path": null,
  "executable": "/path/from/operator/claude", "plan_units": {"five_hour": 1},
  "window": null, "max_concurrency": 1, "wall_seconds": 900,
  "categories": ["independent_review"]
}
```

Same module as Z.ai (`zai`/`claude_headless`) with the Anthropic-native base URL and the
Max login's credentials; `thinking_tokens` maps to the CLI's thinking flag the way the
effort mapping does in `go.py`. Explicit-only for reviews, like `openai`.

## Go models worth qualifying

One credential, many models — each is its own lane id, each earns evidence through a
canary before anything else runs on it: the runner marks a lane `unqualified` for work until its model has an accepted `canary` row in the scorecard, so registering a model
costs nothing (`lane-init` + one tick) until the canary passes. `REASONING_EFFORT` in `lanes/go.py` records what
each model's documented schema honours; models absent from it send no effort field and
say so (`reasoning_effort: unsupported`).

| Lane id | Model | Family | Tier | Note |
| --- | --- | --- | --- | --- |
| `go` | `glm-5.3-flash` | glm | build | The proven route for tightly specified pure functions |
| `go-kimi` | `kimi-k3` | kimi | review | The proven reviewer; needs `reasoning_effort` (default `max` thinks its output away) |
| `go-deepseek` | `deepseek-v4-flash` | deepseek | build | Needs "Enable models hosted in China" in the Go console first; then canary |
| `go-qwen` | `qwen3.8-max` | qwen | build | Third family candidate; canary before use |
| `go-minimax` | `minimax-m3` | minimax | build | Unqualified; canary before use |
| `go-grok` | `grok-4.6` | grok | build | Refuses the OpenAI-compatible endpoint (protocol fact); re-probe only after the endpoint changes |

The tier column is the operator's intent, not evidence: a lane earns its rows by canary and work the
same way whatever tier it carries, and the SOTA lanes above predate the key — the column records
what the routing already wanted. Declaring a tier is only possible once the config key set carries
the key (see Model tiers).

## Cline subscription over HTTP — `cline-http`

```json
"cline-http": {
  "provider": "clinepass", "family": "glm", "model": "z-ai/glm-5.3-flash",
  "kind": "go_http", "credential_path": "/path/from/operator/credential.json",
  "plan_units": {"five_hour": 1}, "window": null, "max_concurrency": 1,
  "wall_seconds": 600, "categories": ["independent_review", "pure_function"]
}
```

The same `go_http` module with `provider: clinepass`: `lanes/go.py` sends the identical
chat-completions request to the ClinePass endpoint, unwraps the `{"data": {...}}` wrapper
it puts around the response document (the wrapper itself is preserved in `native.json`),
and records the response's `provider` field in the verdict. Key path only — no
executable; the credential file is the same 0o600 JSON document `read_key` already
parses. An unknown `provider` in a `go_http` lane refuses before anything is sent.

Plan coverage, observed on the raw API 2026-09-15: only some models are covered by the
subscription — `z-ai/glm-5.3-flash` answered, while `moonshotai/kimi-k3` and
`deepseek/deepseek-v4-flash-0731` returned `402 insufficient_credits` (those are billed
to pay-as-you-go credits unless called through the Cline client). `go.py` classifies a
402 as `refusal: model_not_in_plan`, which the driver never retries — a model outside
the plan is a lane-config fact, not a transient fault. Qualify new models through a
canary row before dispatching work on them.

## Go subscription as an agent — `opencode_cli`

```json
"go-agent": {
  "provider": "opencode", "family": "glm", "model": "opencode/glm-5.3-flash",
  "kind": "opencode_cli", "credential_path": null,
  "executable": "/path/from/operator/opencode", "plan_units": {"five_hour": 1},
  "window": null, "max_concurrency": 1, "wall_seconds": 900,
  "categories": ["pure_function", "tests_multi_file"]
}
```

The same Go subscription `go_http` spends on single calls, run through the `opencode`
CLI as an agent with tools (`opencode run --model <model> --format json --dir <worktree>`
with the brief as the prompt). Evidence is the CLI's own NDJSON event stream, qualified
by `classify_opencode`: every line must parse, exactly one terminal `step_finish` must
echo the asked model, and the last assistant `text` part is the terminal text (event
shapes documented in `opencode_outcomes.py`; worth re-checking against a real capture
before the first dispatch). Digests of the CLI's config (`~/.config/opencode/
opencode.json`) and auth (`~/.local/share/opencode/auth.json`) files are taken before
and after the run, as in the goat lane.

**Policy gate, the operator's decision, not the code's:** the deny-read list currently
names the opencode auth file, and the lane refuses to start on that fact alone — verdict
`credential_denied_by_policy`, nothing spawned, nothing digested. Removing the entry
exposes the key to the model's own shell: the sandbox in this package bounds writes,
not reads, so a workload could read the auth file into its context or a transcript, and
the before/after digest check would still pass — it proves only that the key was not
modified, never that it was not read. The digest discipline exists to detect a tampered
login once the operator decides to allow the read; allowing it is worth one deliberate
edit to `deny-read.json`, made with that sentence in mind.

## Work run outside the grid — `external`

A lane the operator launches by hand (a sandboxed `run_lane.py` on a repo packet, hours
long, well past the ledger's dispatch timeout) never becomes a ledger attempt on its own,
so the scorecard learns nothing from it: the routing evidence for a model comes only from
what the grid dispatched and judged. `inference-grid external --json spec.json` closes
that gap without pretending. It writes one task and one attempt born `completed`, both
stamped `provenance: "operator"`, with no quota reservation, no outbox row and no
admission event; the outcome is recorded in the same call. The receipt must say
`verified_in_lane: true|false` — whether the lane could run its own gate. Work it could
not (a Playwright spec written blind, judged only by the operator's run) is scored on
that run, and `repairs` is the count of fixes it needed. The category may not be
`canary`: qualification is earned only through work the grid itself launched. Since
packet tasks (docs/BOARD.md, "Packet tasks"), the board runs the build→gate→re-enter
loop itself; `external` is now the fallback for work the board could not run.
Each task id is written once. The scorecard reports `external` per row so a reader can
weigh operator-provenance rows against grid-dispatched ones.

```json
{"task": "lane-glm-20260914-t1", "project": "monarch",
 "spec": {"authorized": true, "account": "zai", "model": "glm-5.3-flash", "family": "glm",
          "argv": ["cmd", "--print", "..."], "workspace": "/Users/me/.grid-workspaces/monarch-glm"},
 "receipt": {"verified_in_lane": false, "elapsed_s": 1909, "returncode": 0},
 "category": "e2e-spec", "accepted": false, "repairs": 6,
 "note": "6 of 8 specs failed on the operator's Playwright run; all spec bugs"}
```

Acceptance rate alone cannot say how good a *reviewer* lane is: approving a malformed
verdict counts, finding a real defect does not. Reviewer calibration (docs/BOARD.md,
"Reviewer calibration") closes that with packets whose defects are known — the seed
corpus (`calibration/example/` in this repository; the operator's own corpus lives outside it) replants the defect classes the operator's own gates
caught after approvals (a Playwright mock matching the wrong request path, a DELETE
branch nested under a list-path condition, a count that does not sum, an abatement base
that includes the grant) plus a clean case for false positives — and scores each lane's
settled replies for recall, precision and severity-weighted recall. Run it before
trusting a lane's acceptance rate for `independent_review` routing.
