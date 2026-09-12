# Inference Grid

A small, project-independent control plane for bounded inference jobs. PostgreSQL owns admission and attempt state; RabbitMQ transports attempt IDs; Celery runs trusted adapters. Subscription windows and account aliases are admission constraints, not token prices.

**Status: 0.1.0a1 public-alpha candidate.** Local failure tests run with SQLite. PostgreSQL and RabbitMQ are the production target and have separate integration checks. This is not yet a production service, a credential broker, or a security sandbox. No live provider account is configured in this repository.

## Quick start (no provider calls)

```sh
python3.12 -m venv .venv
. .venv/bin/activate
pip install -e '.[test]'
pytest -q
python examples/demo.py
```

Installing dependencies requires network access unless they are already cached. The tests and demo below use no provider credentials.

The demo creates a temporary ledger, account and workspace, admits one synthetic task, produces an artifact, then demonstrates duplicate-message suppression. It uses no API and spends nothing. `completed` does not mean reviewed or accepted.

## Services

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

Account arguments: `name`, `capacity`, `windows` (remaining quota by explicitly named unit/window), `expires` (Unix freshness deadline), `models`, optional `alias_names` and `observed_at` (the original Unix collection timestamp). Native collectors must supply `observed_at`; omission means a new local operator observation. Replayed observations do not restore locally debited capacity. Run `inference-grid init` after updating an existing prototype database to add the observation table. Task arguments: `task`, `project`, `spec`. See [the specification](docs/SPEC.md) for the task contract, gates and roadmap.

`inference-grid status` shows attempt state. `inference-grid doctor` performs read-only ledger and executable checks; provider authentication and broker health are explicitly not checked. See [cloud hardening](docs/CLOUD-HARDENING.md). `hold-abandoned --json ...` takes an operator-selected `before` timestamp. `accept` records **operator-attested** independent review tied to an exact receipt hash; it does not authenticate a reviewer or verify a provider identity.

## Guarantees and limits

* Account aliases share reservations; every configured quota window must be reserved.
* Tasks and workspaces cannot be claimed concurrently. Task specs are immutable.
* Dispatch intent commits before adapter start. Duplicate queue delivery is a no-op.
* A timeout, worker loss or invalid output does not silently release capacity.
* Artifact content, manifest, terminal reason and requested model must match before completion.
* No automatic provider fallback, inferred currency conversion, hidden retry, merge, publish or deployment.

External exactly-once execution is **not** guaranteed. Database fencing prevents a stale attempt from being accepted locally; provider reconciliation is still necessary. Adapter receipts are trusted evidence from locally approved code, not cryptographic provider attestations. Run untrusted code in a real OS/container sandbox. This worker only separates directories and limits runtime and captured output; it does not prevent filesystem/network access or disk exhaustion.

## Project layout

`ledger.py`: transactions and admission; `scheduler.py`: priority and authorized candidate order; `queue.py`: outbox and Celery; `worker.py`: bounded process and verification; `receipts.py`: strict receipt shape; `aliases.py`: alias resolution; `quota.py`: timestamp/usage validation; `native_receipts.py`: native response structural checks; `tests/`: adverse-path evaluations.

[Cloud-first roadmap](docs/ROADMAP.md) · [Decision and implementation spec](docs/SPEC.md) · [Failure policy](docs/FAILURE_POLICY.md) · [Evaluation record](docs/EVALUATION.md) · [Provider contribution record](docs/CONTRIBUTIONS.md)

## Alpha release scope

This repository is useful for evaluating durable local admission and trusted adapter contracts. Real PostgreSQL, RabbitMQ delivery, worker death and broker application restart have been tested on macOS. Linux CI has passed for the initial publication candidate, including PostgreSQL, RabbitMQ worker loss and container restart. Tagged candidates are rerun before release. Native account collectors and full Go/Cline/Command Code adapters are not bundled yet; their external canaries do not imply they are integrated here. See CONTRIBUTING.md for destructive-test opt-ins.

## Local capacity app

An installable, read-only [capacity PWA](docs/CAPACITY-PWA.md) displays provider headroom and freshness from a sanitized local feed. A separately running sanitized quota feed is required for live data; no native collector is bundled. Start it with `inference-grid-capacity`; the default URL is http://127.0.0.1:8040. Without the feed, the shell can load but live capacity is unavailable.
