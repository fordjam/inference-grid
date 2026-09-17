# Build specification: Inference Grid

## Objective

Increase accepted, useful project work per unit of constrained subscription capacity. Minimize coordinator tokens, repeated context, failed dispatch and human nudges. Utilization is a diagnostic, not the optimization target. A failed cheap job that costs a frontier model more to repair is not a saving.

The project must be independent of any finance application. Project adapters provide ready tasks, authorized inputs and acceptance criteria. Provider adapters normalize native lifecycle records. No project data, account identifiers, cookies or private paths belong in the repository.

## Target decision

Use a SQL ledger (SQLite locally; any SQLAlchemy-supported database in production) as the authoritative policy/attempt ledger, with the board runner dispatching directly to a bounded worker process — no message broker. Build the small policy layer specific to account quotas, aliases, output validation and independent review. Do not fork an entire coding-agent platform for scheduling. Add an HTTP inference proxy such as Plano only if measured routing savings justify it; it is not the task ledger or recovery coordinator.

This decision follows negative-control evaluations of competing claim/dispatch behavior and ambiguous session recovery. The generic repository contains its own repeatable tests, not private pilot records or vendored upstream source.

## Task contract implemented in v0.1

`task` and `project` are stable strings. `spec` contains:

- `authorized: true`: local operator attestation; not an authentication mechanism.
- `model`, `family`: exact permitted model and implementation family.
- `argv`: trusted adapter command, without shell interpolation. Commands and their installed code are an operator trust boundary. Do not put credentials in arguments.
- `workspace`: absolute, dedicated workspace root. Each attempt gets a new child directory.
- `timeout`: 1–3600 seconds; `output_bytes`: 1–10,000,000 captured stdout/stderr bytes and per-artifact limit. Disk/process containment requires a separate sandbox.
- `inputs`: map of safe relative paths to SHA-256 digests; `input_root`: absolute source root when inputs exist; `manifest_sha256`: hash of canonical JSON input map. Input content is checked before dispatch and staged separately.
- Optional `priority` (lower first), `candidates` (ordered account/estimate objects) and `account_aliases` for deterministic routing. All candidates use the same declared model. A different model requires an explicit new task.

The worker passes one JSON request on stdin containing attempt, generation, requested model, manifest hash, input directory and output directory. The adapter writes artifacts and emits exactly one terminal JSON receipt on stdout: `status=completed`, `finish_reason=stop`, `actual_model`, `manifest_sha256`, and nonempty `artifacts` with relative path/SHA-256. Native adapter logs belong in bounded stderr. An adapter must implement its own provider-token/output bound and expose its native completion facts; the worker cannot infer a native token budget from stdout size.

Board task category `packet`: the task file carries, beside the standard keys, a `spec` of exactly `{brief, packet_id, gates, base, max_rounds?}` — the brief document path (the task's own `brief`), a `#### <id>.` heading inside it, code gates (`{name, argv, cwd?, timeout?, env?}`), the base branch, and an optional round bound. Dispatch opens the ledger attempt before the loop, runs the build→gate→re-enter loop in a scratch clone branched from `base`, and settles the attempt from the loop's verdict; on green gates the branch is fetched into the project as `packet/<task-id>` (docs/BOARD.md, "Packet tasks").

## State machine

`queued → dispatching → completed → accepted`; `queued/dispatching → held` on stale admission or uncertainty; `held → abandoned | failed` only through operator `resolve`. Admission and an outbox row commit together. Generation plus state compare-and-set fences completion and duplicate start. Account reservations span five-hour, weekly and other explicitly configured windows. Account aliases cannot be reassigned. Workspace exclusion applies across accounts and projects.

The first start commits dispatch intent. A crash after that point cannot be interpreted as "nothing happened." Redelivery returns a no-op. An operator can mark old dispatches held. There is no automatic release or retry. `resolve` is the auditable administrative step for a held attempt only: outcome `released` (operator attests from native evidence that nothing was consumed; the reservation is dropped, state `abandoned`) or `consumed` (the provider ran or may have run; the reservation is debited, state `failed`). Both free the workspace and account slot and record the original hold reason, resolution reason, operator and evidence digest in one event. Resolution never re-dispatches; new work needs a new task. Automatic native reconciliation remains future work.

Completion conservatively debits reserved estimates. Fresh observations replace the budget snapshot; overlapping refresh/completion can double-count conservatively. Observations now carry an observed_at timestamp: older snapshots and same-timestamp changed content are refused, while exact replay preserves local debits. Delayed observations also retain estimated debits for completions after their sampling timestamp. Collectors must retain the original observation timestamp rather than retimestamp cached data. Actual consumption reconciliation and reset epochs remain required before production budget automation. Unknown quota must not become zero usage or unlimited capacity.

## Delivery priority

The [cloud-first roadmap](ROADMAP.md) is the prioritized work sequence. Supervised lifecycle, trustworthy quota collection, observable refresh and qualified native cloud adapters come first. Capability discovery, execution telemetry, safe recovery, session affinity and outcome-based routing follow. Optional oMLX integration is deferred until cloud operation is qualified. The stages below remain evidence gates, not a claim that all earlier operational work is finished.

## Delivery stages and exit criteria

| Stage | Deliverable | Exit evidence |
| --- | --- | --- |
| 0.1, implemented | Generic ledger, deterministic router, transactional outbox, bounded adapter worker, receipts, local CLI, synthetic demo | Local adverse-path tests and receipt rejection tests pass |
| 0.2, superseded | Multi-process operational qualification (the RabbitMQ/Celery path evaluated here was later deleted in favor of the board runner dispatching directly) | Concurrent multi-process claims, worker kill and database outage with no unauthorized repeat dispatch |
| 0.3 | Go, Cline and Command Code adapter packages | One bounded native job per provider, exact model/finish evidence, known quota provenance, explicit token cap, no paid fallback, locked-screen operation where supported |
| 0.4 | Reconciliation and autonomous service | Native terminal resolution releases only justified reservations; retry budget is shared; auth/429 pause lane; versioned snapshots; dependency-aware tasks; durable fairness and backpressure |
| 0.5 | Review and project integration | Independent reviewer receipt and executable checks bind exact artifact; immutable result store; project board import/export; no automatic merge/deploy without policy |
| 1.0 | Public operational release | Auth/RBAC, migrations, metrics, backups/restore, threat model, license/dependency review, clean secret scan, multi-project fairness and acceptance benchmarks |

## Required next evaluations

Run against a production-shaped database rather than treating SQLite serialization as proof. Kill a worker after dispatch intent and verify external adapter call count does not increase after the runner retries admission. Retry connection refusal only with confirmed absence and budget. Test reset rollover, late quota snapshots, native completion arriving after hold, truncated output, silent model fallback, symlink traversal, same-account aliases and two projects competing fairly.

Track accepted work, failure/rework rate, coordinator tokens, subscription consumption in native units, queue age, held age, review age and fallback count. Persist task-session affinity; don’t reclassify every agent turn. Start with deterministic category routing and measured provider success rates. Add learned routing only after an evaluation dataset demonstrates net improvement including cache loss and repair cost.

## Public-repository boundary

The repository is code/specification only. Credentials stay in a host adapter's secret store; provider identity and permitted payload are explicit local configuration. No browser cookies, session exports, native account logs, proprietary tasks, finance data or board snapshots are included. A future public repository needs an explicit publication step; local construction is not publication approval.
