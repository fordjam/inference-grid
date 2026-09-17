# Inference Grid

**Make useful progress across your AI coding subscriptions without constantly deciding who should work next.**

If you use several AI coding tools, one can run out of allowance while the others sit idle. Each has different models, usage limits and ways of running tasks. Keeping work moving means checking quotas, choosing a provider, handing over context, tracking results and recovering when a session fails.

Inference Grid is being built to coordinate that work across projects. The aim is to assign suitable tasks to available providers, respect their limits, and keep a reliable record of what happened—so you spend less time moving work between tools and more time reviewing useful results.

**Start here:** [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — the pieces, the life of a task, the packet loop and the capacity pipeline as diagrams, and the five things you actually do with it.

## The problem in practice

Imagine you have a feature to implement, tests to write and a code review waiting:

- Your strongest model is close to its weekly limit.
- Another subscription has room to handle a smaller task.
- A previous agent stopped halfway through, and you do not know whether it finished.

The intended workflow is to give Grid the tasks, their priorities and the providers allowed to handle them. Grid should reserve capacity, run suitable work, collect the results and hold uncertain attempts for investigation. A finished response still needs to pass the task's checks and review.

The goal is **more accepted work from the capacity you already have**. Keeping every subscription busy is only useful if its output saves time overall.

## What works today

The [first release](https://github.com/fordjam/inference-grid/releases/tag/v0.1.0a1) is published; `main` carries the newer board work, recorded row by row in [docs/CONTRIBUTIONS.md](docs/CONTRIBUTIONS.md).

Today you can:

- Define tasks, permitted models and account allowances, then run explicitly configured worker commands.
- Reserve capacity across multiple usage windows and prevent duplicate queued deliveries from starting the same attempt again.
- Record outputs, check their identity and contents, and hold uncertain results instead of silently retrying them.
- Run a board of tasks unattended: the runner selects a lane per task, dispatches bounded attempts, runs the coordinator's tests on what comes back, and passes or blocks each task with recorded reasons ([docs/BOARD.md](docs/BOARD.md)).
- Gate acceptance behind cross-family review: the packaged lanes (`zai`, `zcode`, `go`, `go-kimi`, `cline`, `goat`) have each completed board-driven attempts, and an approving review — never a mere pass — accepts the attempt (CONTRIBUTIONS, 2026-09-12/13).
- Retry as a recorded change: `board-new --retry` supersedes the predecessor, moves the staged source link for board-work reviews, and copies standalone reviews as-is.
- See what a board is doing without spending quota: `board-status` resolves live reviews and held attempts (refusal, bounds, artifacts), `--suggest` pre-fills the retry JSON for transport-dead tasks, and `board-tick --dry-run` prints the plan — lane or exact skip reason per ready task — before dispatching anything.
- Land accepted work automatically: on 2026-09-13 the runner accepted its first reviewed attempt and committed the artifact and its record to the project's `grid/inbox` branch (CONTRIBUTIONS, "Live round 2").
- Render the ledger's evidence as a document with `inference-grid evaluation`, use the capacity dashboard with a separately supplied usage feed, and inspect local configuration with `inference-grid doctor`.

**Still true:** connecting your subscriptions is operator configuration (a private `lanes.json` and your own collectors), not a plug-and-play setup. The board runner (`inference-grid board-tick`, looped by `deployments/local/tick_boards.py`) is available; automatic recovery, unified service installation and routing based on measured task quality remain [roadmap work](docs/ROADMAP.md).

This runs trusted commands and is not a security sandbox.

## Try the demo

Use Python 3.12. From a checkout of this repository:

```sh
python3.12 -m venv .venv
. .venv/bin/activate
pip install -e '.[test]'
pytest -q
python examples/demo.py
```

Installation needs network access unless dependencies are cached. The demo needs no provider credentials: it creates a temporary task, produces an output, and shows that delivering the same job again does not rerun it. Expect `completed` followed by `duplicate_or_stale`.

## View capacity

The optional [capacity app](docs/CAPACITY-PWA.md) shows usage, remaining allowances and the age of each reading. Start it with `inference-grid-capacity`, then open http://127.0.0.1:8040.

Live readings require a separately running sanitized quota feed; no native collector is bundled. Without one, the app can open but provider capacity remains unavailable. The [standalone deployment bundle](deployments/capacity/README.md) supports an authenticated hosted dashboard receiving snapshots from your own collector.

## How it works

Grid separates deciding whether work may start from delivering and executing it:

| Component | Responsibility |
| --- | --- |
| Ledger (SQLite) | Stores tasks, reserves account capacity and records attempt state |
| Board runner | Ticks each board, dispatches ready tasks to a lane and runs the bounded worker directly — no broker in between |
| Provider adapters | Translate a provider's native execution and results into Grid's task contract |

Provider adapters are trusted local code; the public package does not contain account credentials.

## Run the runner

```sh
inference-grid init
inference-grid account --json account.json
inference-grid submit --json task.json
inference-grid board-tick --board-dir grid/board --project-root .
```

Run `inference-grid tick` to admit ready tasks and `inference-grid board-tick` (or `deployments/local/tick_boards.py` for the unattended loop over every board) to dispatch and run them; each tick rechecks durable admission. A crashed dispatch is held, not automatically repeated.

When upgrading an existing ledger, run `inference-grid init` before resuming the runner; 0.1.0a2 adds persistent cooldown state. See [cooldown behavior](docs/CLOUD-HARDENING.md).

Account arguments: `name`, `capacity`, `windows` (remaining quota by explicitly named unit/window), `expires` (Unix freshness deadline), `models`, optional `alias_names` and `observed_at` (the original Unix collection timestamp). Native collectors must supply `observed_at`; omission means a new local operator observation. Replayed observations do not restore locally debited capacity. Run `inference-grid init` after updating an existing prototype database to add the observation table. Task arguments: `task`, `project`, `spec`. Outcome arguments (`outcome --json`): `aid`, `category`, `accepted`, optional `usage` map, `repairs`, `note`; `scorecard` aggregates attempts, completions, acceptances, resolutions and reported usage per family/model/category. Lane arguments (`lane --json`): `provider`, `record` (see docs/CLOUD-HARDENING.md). Resolve arguments (`resolve --json`): `aid`, `outcome` (`released` or `consumed`), `reason`, `operator`, optional `evidence` object; only held attempts are accepted and nothing is re-dispatched. See [the specification](docs/SPEC.md) for the task contract, gates and roadmap.

`inference-grid status` shows attempt state. `inference-grid doctor` performs read-only ledger and executable checks; provider authentication is explicitly not checked. See [cloud hardening](docs/CLOUD-HARDENING.md). `hold-abandoned --json ...` takes an operator-selected `before` timestamp and moves any attempt still stuck in `dispatching` past it into `held`. `accept` records **operator-attested** independent review tied to an exact receipt hash; it does not authenticate a reviewer or verify a provider identity.

## Execution boundaries

* Account aliases share reservations; every configured quota window must be reserved.
* Tasks and workspaces cannot be claimed concurrently. Task specs are immutable.
* Dispatch intent commits before adapter start. Duplicate queue delivery is a no-op.
* A timeout, worker loss or invalid output does not silently release capacity.
* Artifact content, manifest, terminal reason and requested model must match before completion.
* No automatic provider fallback, inferred currency conversion, hidden retry, merge, publish or deployment.

External exactly-once execution is **not** guaranteed. Database fencing prevents a stale attempt from being accepted locally; provider reconciliation is still necessary. Adapter receipts are trusted evidence from locally approved code, not cryptographic provider attestations. Run untrusted code in a real OS/container sandbox. This worker only separates directories and limits runtime and captured output; it does not prevent filesystem/network access or disk exhaustion.

## Developer references

`ledger.py`: transactions and admission; `scheduler.py`: priority and authorized candidate order; `worker.py`: bounded process and verification; `board/runner.py`: the tick that dispatches ready tasks to a lane; `receipts.py`: strict receipt shape; `aliases.py`: alias resolution; `quota.py`: timestamp/usage validation; `native_receipts.py`: native response structural checks; `tests/`: adverse-path evaluations.

[Cloud-first roadmap](docs/ROADMAP.md) · [Decision and implementation spec](docs/SPEC.md) · [Failure policy](docs/FAILURE_POLICY.md) · [Evaluation record](docs/EVALUATION.md) · [Provider contribution record](docs/CONTRIBUTIONS.md)

## Validation

Linux CI exercises the SQLite-backed test suite, worker loss, dashboard behavior and the deployment build. Passing these checks does not establish production readiness or qualify every cloud provider. See the [evaluation record](docs/EVALUATION.md), [release scope](docs/RELEASE.md) and [contributing guide](CONTRIBUTING.md) for evidence and test requirements.
