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

**Choosing among lanes of the same tier (B4).** The router is a static tier table, not a
learned score: no acceptance rate, no benchmark prior, no quality-per-dollar bandit. Once
tier, the cross-family rule (a review may not be graded by its own author) and readiness
have narrowed the field, the lane whose account has the most remaining quota this window
wins — the tightest of its configured windows (five_hour/weekly/monthly): the minimum
absolute remaining units, not the capacity dashboard headline's percentage-used reading
of the same "tightest window" idea. Ties break on lane id. A dry run's `score` is that
winning quota number and `quota_rows` names every candidate's reading; neither `explore`
nor `value` appears anywhere in a dry run's output.

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

## Lane policy facts — `lanes-meta.json`

Some lane facts are policy rather than transport: where the provider hosts the model and
what it promises to retain. `lanes/config.py` is provider-authored and admits exactly its
required keys, so those facts live in a sidecar the operator keeps **beside `lanes.json`**
— `lanes-meta.json`, read once per tick:

```json
{"lanes": {
  "go-opencode": {"residency": "us", "retention": "zero",
                  "retention_source": "https://opencode.ai/docs/zen"},
  "go":          {"residency": "unknown", "retention": "unknown"}
}}
```

`residency` is `us | eu | unknown`, `retention` is `zero | days | unknown`, and
`retention_source` is the URL or note the two facts were read from — the record is the
operator's statement, and the source is what makes it auditable. A lane the file does not
name (or a record without the key) reads `unknown` on that key, and a board the operator
has not tagged at all (no sidecar) reads unknown everywhere: absence never satisfies.

A board's tick config (the `board-tick --json` document, or the board's entry in
`tick-all`'s `boards` directory) may carry `require_lane_meta`:

```json
"require_lane_meta": {"residency": ["us", "eu"], "retention": ["zero"]}
```

Every candidate lane — a task's explicit `lanes` list included — whose sidecar record does
not satisfy **every** listed key is dropped before selection with reason `lane_policy`
naming the key (`"detail": "residency unknown not in [us, eu]"`), and the dry run shows
the drop in its plan rows. Unknown never satisfies, so an untagged lane is refused rather
than trusted; when no lane satisfies the requirement the task is refused outright
(`reason: lane_policy`) rather than routed to a lane the board must not use. A malformed
sidecar — unparseable JSON, a key or value outside the shapes above — refuses the whole
tick with the parse error: fail closed, never route on half-read policy. Without
`require_lane_meta` the tick behaves exactly as before the key existed.

## Packaged kinds

| Kind | Module | Family | Contract |
| --- | --- | --- | --- |
| `go_http` | `go` | varies by model | One chat-completions call to the lane provider's endpoint (`opencode` → the OpenCode Go subscription, `clinepass` → ClinePass); `reasoning_effort` per `REASONING_EFFORT`, `max_tokens` capped by the task's thinking budget |
| `goat_cli` | `goat` | glm | Command Code CLI, `--mod` session effort, config-hash guard, `classify_goat` |
| `cline_cli` | `cline` | qwen | Write sandbox, `iteration_end` snapshots with deletion restore, `classify_cline` |
| `claude_headless` | `zai` | claude | Anthropic-compatible base URL, `MAX_THINKING_TOKENS` from the task budget |
| `zcode_cli` | `zcode` | glm | Bundled ZCode CLI, session-DB evidence |
| `codex_cli` | `codex` | openai | Codex CLI headless (`codex exec`); `classify_codex` — see below |
| `opencode_cli` | `opencode` | glm | `opencode run --format json` as an agent with tools; event-stream evidence, config/auth digest guard, `classify_opencode` — see below. The packet loop runs it (`PACKET_KINDS`); `lanes.json` cannot name the kind until `lanes/config.py`'s accepted set grows by one entry (provider-authored; patch in the D2 report) |

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

## Command Code GOAT — a third family on the same account

`goat-qwen` (`qwen/qwen3.8-flash`, family qwen) was added 2026-09-16 beside `goat` (glm) and
`goat-deepseek`: the plan lists 70 models and the account had used two families while every
review of its own GLM builds queued on the Go plan. Same credential, same `plan_units`, its
own canary and review qualification. Hosting and retention unknown, as for the other goat lanes.

## Cline (retired 2026-09-16)

ClinePass was cancelled after five days: 100% weekly and 90% monthly for 4 accepted packets
and 7 failures. Its metering bills every cached read of an agent loop's context, and the CLI
cannot resume a session with a prompt in JSON mode, so each fix round rebuilt the context from
zero (the 5-hour cap fell to two concurrent 50-minute runs). The lane rows are kept in
`lanes.retired-cline-20260916.json` beside `lanes.json`; the section below stays as the record.


```json
"cline": {
  "provider": "cline", "family": "kimi", "model": "cline-pass/kimi-k3",
  "kind": "cline_cli", "executable": "/path/from/operator/cline",
  "categories": ["pure_function", "independent_review"]
}
```

Run only after the weekly reset (the account exhausts quickly); never give Cline size
hints — a passing artifact was once deleted to chase a line count. (While the lane was
live, the ledger seeded a `cline` alias placeholder on `init` for `configure_account` to
fill in; `DEFAULT_ACCOUNTS` is empty now that this was its only entry.)

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
weekly readings plus the monthly credits read against the plan's 70-cap monthly window).
Each observation's windows map to remaining units against the documented caps —
remaining share of the window × the window's unit cap, the same shape the zai and go
accounts already use — and the result, with the observation timestamp, is what
`configure_account` writes for the lane's admission decision. (Until the `cline_cli`
lane's retirement on 2026-09-16, a `cline` account was configured the same way from
`cline-observation.json`/`collect_cline.py`; `configure_cline`, those constants and that
collector are gone along with the lane.)

Because `board_prepare` runs once per pass and a pass can last an hour, the board tick
itself re-reads a stale lane record's observation file before refusing it: the tick config
(the `board-tick --json` document) may carry `observations` — a map of provider (or lane)
id to observation file path — and `output_dir`, the collectors' capacity output directory
whose `<provider>-observation.json` convention the map defaults to. `package_src` names the
`deployments/local/` directory the runner imports `board_prepare.py`'s record builder from.
A lane record older than its `quota_freshness_seconds` is rebuilt from the file, recorded
back into the ledger and re-classified; only a file that is itself older than the window
refuses, with the file's age in the reason. Without these keys the tick behaves as before.

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

## Codex three-seat trio — `codex-luna`, `codex-terra`, `codex-sol`

Three `codex_cli` lanes on three separate Codex CLI logins (`~/.codex`, `~/.codex-seat1`,
`~/.codex-seat2`), each its own lane id so the tier table and the scorecard track them
independently even though they draw on the same underlying account's quota (see
`deployments/local/collect_codex.py`'s `observe_seats`, below).

| Lane id | Model | Tier | Seat home | Note |
| --- | --- | --- | --- | --- |
| `codex-luna` | `gpt-5.6-luna` | build | `~/.codex` | Primary build lane for this trio |
| `codex-terra` | `gpt-5.6-terra` | build | `~/.codex-seat1` | Same categories as `codex-luna`; the operator's convention (not a runner feature — see below) is to name this lane explicitly for a packet's next round when `codex-luna`'s attempt failed its gate, rather than retrying `codex-luna` itself |
| `codex-sol` | `gpt-5.6-sol` | review | `~/.codex-seat2` | Reviews Codex-family builds from a distinct login so a `codex-luna`/`codex-terra` attempt is never graded by its own seat |

```json
"codex-luna": {
  "provider": "codex", "family": "openai", "model": "gpt-5.6-luna",
  "kind": "codex_cli", "credential_path": "/path/from/operator/.codex/auth.json",
  "executable": "/path/from/operator/codex", "plan_units": {"five_hour": 1},
  "window": null, "max_concurrency": 1, "wall_seconds": 900,
  "categories": ["pure_function", "independent_review"]
},
"codex-terra": {
  "provider": "codex", "family": "openai", "model": "gpt-5.6-terra",
  "kind": "codex_cli", "credential_path": "/path/from/operator/.codex-seat1/auth.json",
  "executable": "/path/from/operator/codex", "plan_units": {"five_hour": 1},
  "window": null, "max_concurrency": 1, "wall_seconds": 900,
  "categories": ["pure_function", "independent_review"]
},
"codex-sol": {
  "provider": "codex", "family": "openai", "model": "gpt-5.6-sol",
  "kind": "codex_cli", "credential_path": "/path/from/operator/.codex-seat2/auth.json",
  "executable": "/path/from/operator/codex", "plan_units": {"five_hour": 1},
  "window": null, "max_concurrency": 1, "wall_seconds": 900,
  "categories": ["independent_review"]
}
```

`tier` is written above as intent (matching the "Model tiers" table's own convention) but
is not yet a field `lanes/config.py` will accept — see that section for the pending
key-set change.

**The `executable` field cannot select the seat, and a same-executable wrapper alone does
not work either — read this before wiring three seats up.** `lanes/config.py` has no
env-map field a lane record could carry (`allowed` is a fixed key set), so `CODEX_HOME`
cannot be declared as a lane field today. The fallback the operator might reach for — an
`executable` pointing at a small wrapper script that does
`exec env CODEX_HOME=~/.codex-seat1 /path/to/codex "$@"` — looks plausible but does not
actually work with this lane as shipped: `lanes/codex.py::run()` builds its sandbox
profile from a Python-level `home` parameter (`extra_write_roots=(home / ".codex",)`),
and `lanes/runner.py`'s dispatch never passes `home` for the `codex` kind — its `options`
dict overrides `executable` for `zai` only, so every `codex_cli` attempt sandboxes against
the real `$HOME/.codex`, whatever the wrapper's own `CODEX_HOME` says. A wrapper's writes
to `~/.codex-seat1/` would be sandbox-denied, and `codex-luna`/`codex-terra`/`codex-sol`
attempts running concurrently would otherwise race on the one real `~/.codex`.

Landing per-seat isolation is **not** a `lanes/runner.py`-only change — a first draft of
this section proposed passing `home=Path(lane["credential_path"]).parent.parent` at the
dispatch site and left `lanes/codex.py` untouched; on review that resolves to the same
real `~/.codex` for all three lanes; regardless of `home`, `run()` always derives its
sandbox root and effective login directory as `home / ".codex"` — a fixed, one-name-only
suffix — so no value of `home` alone can point it at a seat directory named
`.codex-seat1`. Two designs would actually work, and the choice affects what shape the
operator's `~/.codex-seat*` directories need to be in:

1. **Reshape the seats to fit the existing convention.** Keep `lanes/codex.py::run()`
   unchanged; instead of `~/.codex`, `~/.codex-seat1`, `~/.codex-seat2` directly under the
   operator's real home, each seat becomes its own fake home whose only child is a
   `.codex` directory (e.g. `~/.grid-codex-homes/luna/.codex`,
   `.../terra/.codex`, `.../sol/.codex` — the current `~/.codex-seat1` content moved
   to `.../terra/.codex`). `lanes/runner.py` then only needs `options = {"home":
   Path(lane["credential_path"]).parent.parent}` for the `codex` kind, matching the
   pattern above (now correct because the parent-of-parent really is the fake home). No
   `lanes/codex.py` change. This is the smaller change, but it means restructuring
   `deployments/local/collect_codex.py`'s own `default_homes()` / `codex_homes` config
   to the new paths too, everywhere they're used.
2. **Make `lanes/codex.py::run()` take the seat directory directly**, independent of
   `home` — e.g. a `codex_home=None` keyword defaulting to `home / ".codex"` when absent,
   used for both the sandbox `extra_write_roots` and (if the installed Codex CLI honours
   it — **unconfirmed**, matching this document's existing "pending operator
   confirmation" caveat on the single `codex` lane above; check before relying on it) a
   `CODEX_HOME` environment variable alongside the existing `HOME`. `lanes/runner.py`
   then passes `options = {"codex_home": Path(lane["credential_path"]).parent}` for the
   `codex` kind — that parent (`~/.codex-seat1`) is exactly the seat directory as the
   table above already names it, no reshaping needed.

Either way this is a coordinator edit to package code (not the operator's private
`lanes.json`), in the same spirit as the pending `tier` and `opencode_cli` key-set
patches above, sized past what this row's brief (docs + the collector's read side) covers
— it is not applied in this checkout. **Until one of these two lands, do not run more
than one of `codex-luna`/`codex-terra`/`codex-sol` at a time**: each `max_concurrency: 1`
above bounds concurrency *within* one lane, not across the three, and as configured today
all three attempts would sandbox against the same real `~/.codex` regardless of which
seat's login the operator intended.

**Canary.** Bring each lane up the same way every lane does (top of this document):
`inference-grid lane-init --json {"lane_id": "codex-luna", "board_dir": ...}`, then one
tick — repeated for `codex-terra` and `codex-sol`. `lane_init` writes the brief at
`grid/briefs/canary-<lane-id>.txt` (content: the fixed `CANARY_BRIEF`, "Reply with exactly
OK") and the matching task at `<board_dir>/canary-<lane-id>.json`; it refuses only when
that task file already exists (the brief itself is overwritten unconditionally, no
exists-check). No canary brief or task for these three lanes is pre-created in this
repository: `board_dir` is real board state (this run's hard rule against touching `grid/`
board state, plus this project's own rule that runtime board state lives outside product
repos) that only the operator's own `lane-init` run can correctly place.

**Collector.** `deployments/local/collect_codex.py`'s `observe` (unchanged) keeps reporting
one merged reading — first home that answers — for the account-level dashboard figure. A
new `observe_seats` reads every configured home without stopping at the first success and
returns one reading per seat, folded into `codex-observation.json` under a `seats` key by
`write(obs, config, seats=...)` (additive; a reader that only knows the old shape is
unaffected). This is what shows a caller which of the three seats' logins is actually
healthy — `codex-luna`'s own login expiring must not read as "codex is fine" just because
`codex-terra`'s home still answers.

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
| `go-qwen` | `qwen3.8-flash` | qwen | review | Registered 2026-09-16 as the second non-GLM reviewer (the cross-family rule had put every review of GLM-built work on `go-kimi`); canary and review qualification authored, flash tier because a review is one call |
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

Model id namespace (L6): ClinePass bills vendor ids to credits; the subscription
draws on the `cline-pass/` namespace instead, so `go.py` puts `cline-pass/<bare id>`
on the wire while the lane record keeps the canonical vendor id — the runner's model
match and the scorecard key on it, and the verdict carries `wire_model` whenever the
two differ. The endpoint may echo either id in its response; both name the one model,
and a different model is still refused. This is why `canary-cline-http` failed with
HTTP 429 on 2026-09-16: the vendor id `z-ai/glm-5.3-flash` was served from a free
tier with a daily cap, which the first canary exhausted.

Plan coverage, observed on the raw API 2026-09-15: the pass namespace is what the
subscription serves — the first canary's `429` on the vendor id
(`grid/board/canary-cline-http.json`) and the `402 insufficient_credits` answers for
`moonshotai/kimi-k3` and `deepseek/deepseek-v4-flash-0731` both came from the vendor
id space (those two are billed to pay-as-you-go credits unless called through the
Cline client). `go.py` classifies a 402 as `refusal: model_not_in_plan`, which the
driver never retries — a model outside the plan is a lane-config fact, not a transient
fault. Qualify new models through a canary row before dispatching work on them.

## Go subscription as an agent — `opencode_cli`

```json
"go-opencode": {
  "provider": "opencode", "family": "glm", "model": "opencode/kimi-k3",
  "kind": "opencode_cli", "credential_path": null,
  "executable": "/path/from/operator/opencode", "plan_units": {"five_hour": 1},
  "window": null, "max_concurrency": 1, "wall_seconds": 900,
  "categories": ["pure_function", "tests_multi_file"]
}
```

The lane facts that are policy rather than transport: `residency: "us"`, `retention:
"zero"`, `retention_source: "https://opencode.ai/docs/zen"` — OpenCode Zen hosts every
model in the US under a zero-retention policy, and the Go plan's table marks GLM-5.3-Flash,
Kimi K3 and Qwen3.8 Max "0 days / not used for training", which makes the Go lanes the only
ones an operator can point at a T1 repository (COT) that requires US/EU hosting and zero
retention. `lanes/config.py`'s accepted key set has no place for those three (provider-
authored), so the operator keeps them in the sidecar beside `lanes.json`
(`lanes-meta.json`, see "Lane policy facts" above); a board can then require them with
`require_lane_meta`, and without a record a reader must assume unknown.

The same Go subscription `go_http` spends on single calls, run through the `opencode`
CLI as an agent with tools (`opencode run --model <model> --format json --dir <worktree>`
with the brief as the prompt). Evidence is the CLI's own NDJSON event stream, qualified
by `classify_opencode`: every line must parse, exactly one terminal `step_finish` must
echo the asked model, and the last assistant `text` part is the terminal text (event
shapes documented in `opencode_outcomes.py`; worth re-checking against a real capture
before the first dispatch). Digests of the CLI's config (`~/.config/opencode/
opencode.json`) and auth (`~/.local/share/opencode/auth.json`) files are taken before
and after the run, as in the goat lane.

**Packets.** `opencode_cli` is in the packet loop's `PACKET_KINDS`: the adapter
(`lanes/packet.py::OpencodeAdapter`) starts `opencode run --model <provider/model>
--format json` non-interactive, reads the session id from the JSON stream
(`sessionID`), and a fix round re-enters it with `--session <id>`. The sandbox grants
no home write root for this kind — the CLI's state directory (`.opencode/`) lives under
the clone, inside the workspace's own write root — and admission carries the one-shot
lane's policy gate: a deny-read list covering the auth file refuses
(`credential_denied_by_policy`) before anything spawns.

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
settled replies for recall, precision and severity-weighted recall. B4 stopped feeding
this into routing (the router is a static tier table now); what a calibration run scores
still lands on the needs-you page's reviewer-recall panel, an operator signal for
deciding whether to keep trusting a reviewer lane by hand, not an input the tick reads.
