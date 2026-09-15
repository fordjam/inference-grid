# Lane reference

Documentation only: the private `lanes.json` stays the operator's (validated by
`inference_grid.lanes.config`). Credential paths below are placeholders — the real paths
live only in the operator's `lanes.json`, mode 0600. Every lane earns its rows the same
way: `inference-grid lane-init --json {"lane_id": …, "board_dir": …}`, then one tick.

## Packaged kinds

| Kind | Module | Family | Contract |
| --- | --- | --- | --- |
| `go_http` | `go` | varies by model | One chat-completions call to the OpenCode Go endpoint; `reasoning_effort` per `REASONING_EFFORT`, `max_tokens` capped by the task's thinking budget |
| `goat_cli` | `goat` | glm | Command Code CLI, `--mod` session effort, config-hash guard, `classify_goat` |
| `cline_cli` | `cline` | qwen | Write sandbox, `iteration_end` snapshots with deletion restore, `classify_cline` |
| `claude_headless` | `zai` | claude | Anthropic-compatible base URL, `MAX_THINKING_TOKENS` from the task budget |
| `zcode_cli` | `zcode` | glm | Bundled ZCode CLI, session-DB evidence |
| `codex_cli` | `codex` | openai | Codex CLI headless (`codex exec`); `classify_codex` — see below |
| `opencode_cli` | `opencode` | glm | `opencode run --format json` as an agent with tools; event-stream evidence, config/auth digest guard, `classify_opencode` — see below. `lanes.json` cannot name the kind until `lanes/config.py`'s accepted set grows by one entry (provider-authored; patch in the D2 report) |

## Command Code GOAT

```json
"goat": {
  "provider": "goat", "family": "glm", "model": "glm-5.3-flash",
  "kind": "goat_cli", "credential_path": "/path/from/operator/config.json",
  "executable": "/path/from/operator/commandcode", "plan_units": {"five_hour": 1, "weekly": 1},
  "window": null, "max_concurrency": 1, "wall_seconds": 900,
  "categories": ["tests_multi_file", "fixtures_multi_file"]
}
```

Reserve for multi-file work; every call carries ~16–25k input tokens of fixed CLI
overhead, so small jobs are wasted on it.

## Cline

```json
"cline": {
  "provider": "cline", "family": "qwen", "model": "cline-pass/qwen3.8-max",
  "kind": "cline_cli", "credential_path": "/path/from/operator/auth.json",
  "executable": "/path/from/operator/cline", "plan_units": {"weekly": 1},
  "window": null, "max_concurrency": 1, "wall_seconds": 600,
  "categories": ["pure_function", "independent_review"]
}
```

Run only after the weekly reset (the account exhausts quickly); never give Cline size
hints — a passing artifact was once deleted to chase a line count. The ledger seeds a
`cline` alias placeholder on `init`; `configure_account` fills it in.

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

| Lane id | Model | Family | Note |
| --- | --- | --- | --- |
| `go` | `glm-5.3-flash` | glm | The proven route for tightly specified pure functions |
| `go-kimi` | `kimi-k3` | kimi | The proven reviewer; needs `reasoning_effort` (default `max` thinks its output away) |
| `go-deepseek` | `deepseek-v4-flash` | deepseek | Needs "Enable models hosted in China" in the Go console first; then canary |
| `go-qwen` | `qwen3.8-max` | qwen | Third family candidate; canary before use |
| `go-minimax` | `minimax-m3` | minimax | Unqualified; canary before use |
| `go-grok` | `grok-4.6` | grok | Refuses the OpenAI-compatible endpoint (protocol fact); re-probe only after the endpoint changes |

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
`canary`: qualification is earned only through work the grid itself launched.
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
