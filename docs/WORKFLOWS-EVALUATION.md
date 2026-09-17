# Workflow tooling evaluation — sssf and claudexor

**Decision: borrow the shape from `disler/super-simple-software-factory` (code owns phase sequencing, agents return typed envelopes, gates are code between phases); adopt neither package. `razzant/claudexor` is neither — its product surface doesn't fit and the grid already has its useful idea.**

## `disler/super-simple-software-factory` (sssf)

A Claude Code skill (`.claude/skills/sssf/`) that stamps a Python scaffold — "ADW scripts" (AI Developer Workflows) — into any repo. Its central claim, matching C6's brief almost word for word: **code owns sequencing, retries, and acceptance; an agent owns only the work inside one bounded phase.**

Mechanics:
- A run is a sequence of `phase()` blocks, each `kind="agent"` or `kind="code"`, in one swim lane per owner (engineer/code/agent).
- An agent phase (`ph.call(AgentCall(output_type=..., prompt=..., gates=[...]))`) gets a prompt, must return one parsed JSON **envelope** matching a Pydantic type, and is checked by **gates** — callables run after the fact against the envelope's own claims (`artifacts_exist`, `files_non_empty`, `tests_pass(...)`). A gate failure or unparsed JSON **re-prompts the same session** with a correction — never a cold restart.
- Every event (phase start/end, tool call, gate result, envelope) streams into one SQLite db (`sssf.db`) as it happens; a bundled Vue/Bun UI polls it.
- Ships five starter agents (planner, builder, scout, reviewer, documenter) and twelve starter ADW scripts as templates you edit in place.
- Runs on `pi` (a third-party coding-agent CLI), not Claude Code directly; needs `uv`, `bun`, and per-agent provider API keys (OpenRouter/Fireworks/OpenAI in the starter roster).

## `razzant/claudexor`

A commercial-grade, cross-vendor **control plane for interactive agent CLIs** (Claude Code, Codex, Cursor, OpenCode, Antigravity): a macOS app plus an npm daemon (`claudexord`) with a control API, credential-profile management, quota-aware account rotation, best-of-N racing across harnesses with cross-model review/arbitration, protected-path approvals, remote SSH execution, and a signed self-update mechanism for the daemon binary.

Its "workflow" unit is a `mode` (`ask`/`plan`/`agent`) plus strategy flags (`--n`, `--attempts`, `--until-clean`, `--delegate`), not a phase graph a repo owns — there's no equivalent of an ADW script or a packet lane file checked into the target repo. Review is a flag on a run (`--review`, a reviewer panel), not a gate between named phases. The nearest thing to the grid's routing is its quota-aware credential-profile rotation and cross-family reviewer panel.

## What the grid already has

- **Packet loop with gates as code** (`lanes/packet.py:build_loop`): agent round → `run_gates` (pytest, ruff, home-paths, commit — all code, all module-level so a driver writes no script) → on failure, a deterministic `fix_prompt` is built from the gate tails and fed back.
- **Same-session repair**: `build_loop` resumes the *same* adapter session (`adapter.resume(session_id, fix_prompt)`) on every fix round, never a cold restart — sssf's correction loop, already built.
- **Cross-family review as a gate**: `board/branch_review.py` stages a diff/patch for an `independent_review` task; `ledger.accept()` refuses acceptance unless `reviewer_family != task family` and `verdict == "approved"` — enforced in the ledger, not a flag.
- **The ledger**: one `events` table already recording every admission, dispatch, gate outcome, review verdict and land as an auditable row (`ledger.event`), which is the same "one data path, SQLite" idea sssf's tracer implements from scratch.
- **Scout**: `lanes/scout.py:orient` already produces the deterministic pre-brief orientation (recent commits, referencing tests, prior reports) sssf's `scout` agent phase does by prompting a model for the same facts.

The one piece genuinely missing is sssf's outer shape: **one script per work type that names its phases and runs them in order**, with a **typed envelope** passed between phases instead of ad hoc dict-shaped verdicts. Today the packet loop is one generic runner parameterized by a task's category; nothing declares "a chore is scout→build→gates→review→land-request" as its own sequence.

## What each would replace

- **sssf, adopted whole**: would replace the packet loop, the board runner's dispatch logic, and the ledger's events table with its own SQLite tracer — a rewrite of working, tested machinery to gain a shape the grid can build directly on top of what exists.
- **sssf, shape borrowed**: replaces nothing; adds one `workflows/<kind>.py` per work type that calls the existing scout, packet lane, gates, and branch review as phases, with a small typed envelope module.
- **claudexor**: would replace the packaged-lane adapters (`lanes/zai.py`, `lanes/go.py`, `lanes/goat.py`) and the static tier table (B4) with its daemon's own account rotation and best-of-N racing — a second credential store, a second daemon, a second review mechanism layered over ones the grid just finished building per B2/B3/B4. Nothing in the plan calls for interactive multi-harness racing; the grid's problem is unattended packets on subscriptions already paid for.

## Reasons for the decision

1. **C6's own wording is sssf's architecture.** "One deterministic Python script per workflow type ... agents as bounded phases returning typed envelopes and code gates between phases" describes `run.phase(...)` / `ph.call(AgentCall(output_type=...))` closely enough that copying the *shape* is the fast path, not a coincidence to design around.
2. **The primitives sssf's phases would call already exist and are tested** (scout, gates, same-session repair, review-as-a-gate, the ledger's events table). Adopting sssf's package means either running two ledgers side by side or migrating tested code to satisfy a new framework's assumptions (`pi`, its own SQLite schema) for no capability gain.
3. **Adopting sssf's package adds a runtime dependency** (`pi`, `bun`, `uv`) the grid doesn't otherwise need, and a second observability surface (its trace UI) when rule 2 of `00-PORTFOLIO.md` already puts board/runtime state under `~/.local/share/inference-grid/state/<repo>/`.
4. **Claudexor solves a different problem.** It is a personal control plane for *interactive* work across paid subscriptions with a GUI, credential profiles, and a signed auto-updating daemon — exactly the kind of standing infrastructure `00-PORTFOLIO.md`'s "no silent failures" and "$34/month plus Claude Max" framing is trying to shrink, not add to. Its one exportable idea — quota-aware account rotation with typed budget accounting — the grid already has in `ledger.py`'s account/window/cooldown model.
5. **No new tables, per this row's own gate.** Building on the existing ledger and packet lane keeps the pilot's `workflows/chore.py` inside the "typed JSON envelope between phases, every phase logged to the ledger's events table, no new tables" constraint; adopting either package's own event store would violate it on day one.

Net: `workflows/chore.py` (the pilot row) is the shape borrow in practice — scout, build, gates, review, land-request as four ordinary Python functions in a fixed order, each phase's envelope written and logged through `ledger.event`, calling the packet lane and branch review that already exist.
