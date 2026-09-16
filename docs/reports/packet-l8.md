# GLM-5.3-Flash lane report — packet L8: boards that require hosting and retention guarantees (2026-09-16)

Lane: GLM-5.3-Flash (packet lane). Base: `a6d6b7f` (the tip of `glm/work` at branch time).
Branch: `packet/packet-l8`, one commit, not pushed.

## Why

COT's board rules require US/EU hosting and zero retention. Until now that policy was
enforced only by which lanes a task happened to name — one task file with a wrong `lanes`
list would have routed T1 code to a lane whose provider keeps prompts, and nothing in the
tick would have said a word. The facts themselves could not even be written down where the
grid reads them: `lanes/config.py` (provider-authored, integrated unmodified) admits
exactly its required keys, so `residency`/`retention` cannot live in `lanes.json`.

## What landed

**`src/inference_grid/lanes/meta.py`** (new) — the `lanes-meta.json` sidecar: the file the
operator keeps beside `lanes.json` for lane facts that are policy rather than transport.

- `{"lanes": {"<lane id>": {"residency": "us|eu|unknown", "retention":
  "zero|days|unknown", "retention_source": "<url or note>"}}}`. A record may carry any
  subset of the three keys; `retention_source` is free text (the URL or note the two facts
  were read from — the record is the operator's statement, the source makes it auditable).
- `load_lane_meta(lanes_path)` reads the sidecar once beside `lanes_path`; an absent file
  reads as no records (unknown everywhere), never an error. A malformed one — unparseable
  JSON, an unknown key, a value outside the enums, an empty `retention_source` — raises
  with the parse error, and the tick that called it refuses the whole pass: fail closed.
- Missing lanes read unknown, and unknown never satisfies: an untagged lane is refused
  rather than trusted. `validate_requirement` refuses a requirement that lists `unknown`
  among its allowed values (it could only widen the gate back open), a key the sidecar
  does not carry, or an empty/non-list value.

**`lanes/route.py`** — the `lane_policy` filter, first in the pipeline before budget and
tier: the board's `require_lane_meta` (carried on the task dict the way `budget` is)
filters every candidate — a task's explicit `lanes` list included, which is the point:
compliance stops depending on which lanes a task names. A lane whose `lane_meta` record
misses any listed key is dropped with reason `lane_policy` naming the key
(`"detail": "residency unknown not in [us, eu]"`); when no candidate satisfies, the
answer is lane None, reason `lane_policy` — the tier filter never empties the offer, the
policy one deliberately can. A malformed requirement raises ValueError. Without the key
route behaves exactly as before L8, stray records or no records: the drop rows for
policy come first in `dropped`, then budget, then tier.

**`board/runner.py::tick`** — reads the sidecar once per tick beside `lanes_path` (a
malformed file refuses the pass whatever the board requires) and merges each tagged
lane's record into its lane view entry (`lane_meta`, the way `tier` rides the view); the
new `require_lane_meta` parameter rides the task dict route receives. The dry run needs
no new output shape: plan rows already carry `candidates`/`dropped`, so a policy refusal
arrives with its reason and the key named.

**Plumbing** — `cli.board_tick` gains the named parameter and the multi-board key filter
(so a `tick-all` config carries it; `tick_all` already splats config keys through),
and `boards.board_snapshot` passes the config's requirement into the dashboard's dry run,
so the planned rows show the `lane_policy` drops the real tick would make. A malformed
requirement in a dashboard config degrades to an empty plan (the existing except), not a
crashed snapshot.

**`board/new.py::lane_init`** — the returned dict now documents the sidecar (a
`lane_meta` key naming the file, the enums and the unknown reading), so the one command
every lane starts from says where policy facts go.

**`docs/LANES.md`** — new section "Lane policy facts — `lanes-meta.json`" (the shape, the
requirement, the fail-closed rules, the dry-run visibility); the opencode section's
"until that lands" sentence now points at the landed sidecar.

## The operator's entry

```json
// lanes-meta.json, beside lanes.json
{"lanes": {
  "go-opencode": {"residency": "us", "retention": "zero",
                  "retention_source": "https://opencode.ai/docs/zen"}
}}
```

and, in the board's tick config:

```json
"require_lane_meta": {"residency": ["us", "eu"], "retention": ["zero"]}
```

`lanes/config.py` stays untouched (provider-authored): the sidecar is the whole point,
no key-set patch is needed or wanted.

## Tests

- **`tests/test_lane_meta.py`** (new, 11 offline cases): the absent-file reading; the
  sidecar loading beside a lanes path; the malformed file refusing with the parse error;
  the validator refusing junk shapes/values and accepting key subsets; requirement
  validation incl. the `unknown`-cannot-be-allowed rule; the matcher (tagged lane
  satisfies, unknown and untagged drop with the key named, absent key fails the other
  key, first failing key named, days-vs-zero).
- **`tests/test_route.py`** (+11): `LanePolicyTests` — us/zero offers only tagged lanes
  with the untagged drop naming residency; unknown written down still never satisfies;
  days dropped where zero is required; no satisfying lane refuses the task outright;
  explicit lists filtered too; policy drops ordering with budget and tier in one row;
  stray `lane_meta` without a requirement changing nothing; malformed requirements
  refusing. Plus three tick dry-run tests on the world fixture: the board requirement
  applied from a real sidecar file (read once per tick, asserted by counting loads across
  two tasks; the retention-miss refusal naming the key in the plan row), a malformed
  sidecar refusing the whole tick with the parse error, and no sidecar + no requirement
  behaving exactly as today.
- Full suite (`--ignore=tests/test_calibration.py`; that file fails to *collect* under
  this environment's deny-read policy at the base too): the failing set is byte-identical
  to `a6d6b7f` (scratch worktree run, `FAILED`/`ERROR` lines diffed) — 88 inherited
  sandbox denials, zero new; passes 658 → 680.
- `ruff format` and `ruff check` clean on all eight changed files.
- No provider-authored file touched (`lanes/config.py`, `lanes/select.py`,
  `lanes/sandbox.py`, `board/task.py`, `observation.py`, `goat_outcomes.py`,
  `remaining_units.py`, `provider_report.py`, `lane_readiness.py`, `flash_window.py` all
  unchanged). No new credential access — the sidecar carries policy facts, not secrets,
  and nothing in it is read outside the tick's one load. No absolute home path or e-mail
  address in the diff.
