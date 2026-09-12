# Inference Grid

**Make useful progress across your AI coding subscriptions without constantly deciding who should work next.**

If you use several AI coding tools, one can run out of allowance while the others sit idle. Each has different models, usage limits and ways of running tasks. Keeping work moving means checking quotas, choosing a provider, handing over context, tracking results and recovering when a session fails.

Inference Grid is being built to coordinate that work across projects. The aim is to assign suitable tasks to available providers, respect their limits, and keep a reliable record of what happened—so you spend less time moving work between tools and more time reviewing useful results.

## The problem in practice

Imagine you have a feature to implement, tests to write and a code review waiting:

- Your strongest model is close to its weekly limit.
- Another subscription has room to handle a smaller task.
- A previous agent stopped halfway through, and you do not know whether it finished.

The intended workflow is to give Grid the tasks, their priorities and the providers allowed to handle them. Grid should reserve capacity, run suitable work, collect the results and hold uncertain attempts for investigation. A finished response still needs to pass the task's checks and review.

The goal is **more accepted work from the capacity you already have**. Keeping every subscription busy is only useful if its output saves time overall.

## What works today

**Experimental alpha:** the orchestration core is available, and the broader cloud-provider workflow is still being built. The [first release](https://github.com/fordjam/inference-grid/releases/tag/v0.1.0a1) is published; `main` also contains newer hardening work.

Today you can:

- Define tasks, permitted models and account allowances, then run explicitly configured worker commands.
- Reserve capacity across multiple usage windows and prevent duplicate queued deliveries from starting the same attempt again.
- Record outputs, check their identity and contents, and hold uncertain results instead of silently retrying them.
- Run a demonstration without calling a model or spending inference capacity.
- Use a capacity dashboard with a separately supplied usage feed, and inspect local configuration with `inference-grid doctor`.
- Persist account cooldowns and stop new inference from starting before its deadline.

**Connecting your subscriptions is not yet a plug-and-play setup.** Native collectors and complete provider adapters are not bundled. A [dispatcher supervisor](docs/SERVICE.md) is available; automatic recovery, unified service installation and routing based on measured task quality remain [roadmap work](docs/ROADMAP.md). Small external-provider trials help test the design, but do not establish a complete integration.

This alpha is for developers evaluating or extending the system. It runs trusted commands and is not a security sandbox.

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
| PostgreSQL | Stores tasks, reserves account capacity and records attempt state |
| RabbitMQ | Delivers queued attempt identifiers to workers |
| Celery workers | Run configured adapters with runtime and output bounds |
| Provider adapters | Translate a provider's native execution and results into Grid's task contract |

SQLite supports the local demo and evaluation tests. Provider adapters are trusted local code; the public package does not contain account credentials.

## Run the worker services

```sh
cp .env.example .env
# Set a local development database password in .env.
docker compose up -d --wait
export GRID_DATABASE_URL='postgresql+psycopg://grid:YOUR_PASSWORD@localhost:54329/grid'
export GRID_BROKER_URL='amqp://guest:guest@localhost:56729//'
inference-grid init
celery -A inference_grid.queue:app worker --concurrency=2 --loglevel=INFO
```

In another terminal, activate the same virtual environment and export the same `GRID_DATABASE_URL` and `GRID_BROKER_URL` values before submitting work. These exports are shell-local; `.env` supplies Docker Compose settings but is not automatically loaded by the CLI. Otherwise the CLI can use a different SQLite ledger and the default broker port.

Submit account/task JSON files with `inference-grid account --json account.json` and `inference-grid submit --json task.json`. Run `inference-grid tick` to admit ready tasks and `inference-grid publish` to flush the transactional outbox. A service manager may run these commands periodically; each tick rechecks durable admission. A duplicate publish cannot restart an already-started attempt. A crashed dispatch is held, not automatically repeated.

When upgrading an existing ledger, run `inference-grid init` before resuming workers; 0.1.0a2 adds persistent cooldown state. See [cooldown behavior](docs/CLOUD-HARDENING.md).

Account arguments: `name`, `capacity`, `windows` (remaining quota by explicitly named unit/window), `expires` (Unix freshness deadline), `models`, optional `alias_names` and `observed_at` (the original Unix collection timestamp). Native collectors must supply `observed_at`; omission means a new local operator observation. Replayed observations do not restore locally debited capacity. Run `inference-grid init` after updating an existing prototype database to add the observation table. Task arguments: `task`, `project`, `spec`. Outcome arguments (`outcome --json`): `aid`, `category`, `accepted`, optional `usage` map, `repairs`, `note`; `scorecard` aggregates attempts, completions, acceptances, resolutions and reported usage per family/model/category. Lane arguments (`lane --json`): `provider`, `record` (see docs/CLOUD-HARDENING.md). Resolve arguments (`resolve --json`): `aid`, `outcome` (`released` or `consumed`), `reason`, `operator`, optional `evidence` object; only held attempts are accepted and nothing is re-dispatched. See [the specification](docs/SPEC.md) for the task contract, gates and roadmap.

`inference-grid status` shows attempt state. `inference-grid doctor` performs read-only ledger and executable checks; provider authentication and broker health are explicitly not checked. See [cloud hardening](docs/CLOUD-HARDENING.md). `hold-abandoned --json ...` takes an operator-selected `before` timestamp. `accept` records **operator-attested** independent review tied to an exact receipt hash; it does not authenticate a reviewer or verify a provider identity.

## Execution boundaries

* Account aliases share reservations; every configured quota window must be reserved.
* Tasks and workspaces cannot be claimed concurrently. Task specs are immutable.
* Dispatch intent commits before adapter start. Duplicate queue delivery is a no-op.
* A timeout, worker loss or invalid output does not silently release capacity.
* Artifact content, manifest, terminal reason and requested model must match before completion.
* No automatic provider fallback, inferred currency conversion, hidden retry, merge, publish or deployment.

External exactly-once execution is **not** guaranteed. Database fencing prevents a stale attempt from being accepted locally; provider reconciliation is still necessary. Adapter receipts are trusted evidence from locally approved code, not cryptographic provider attestations. Run untrusted code in a real OS/container sandbox. This worker only separates directories and limits runtime and captured output; it does not prevent filesystem/network access or disk exhaustion.

## Developer references

`ledger.py`: transactions and admission; `scheduler.py`: priority and authorized candidate order; `queue.py`: outbox and Celery; `worker.py`: bounded process and verification; `receipts.py`: strict receipt shape; `aliases.py`: alias resolution; `quota.py`: timestamp/usage validation; `native_receipts.py`: native response structural checks; `tests/`: adverse-path evaluations.

[Cloud-first roadmap](docs/ROADMAP.md) · [Decision and implementation spec](docs/SPEC.md) · [Failure policy](docs/FAILURE_POLICY.md) · [Evaluation record](docs/EVALUATION.md) · [Provider contribution record](docs/CONTRIBUTIONS.md)

## Validation

Linux CI exercises SQLite and PostgreSQL tests, RabbitMQ delivery, worker loss, broker container restart, dashboard behavior and the deployment build. Passing these checks does not establish production readiness or qualify every cloud provider. See the [evaluation record](docs/EVALUATION.md), [release scope](docs/RELEASE.md) and [contributing guide](CONTRIBUTING.md) for evidence and test requirements.
