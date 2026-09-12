# Roadmap: reliable cloud inference first

Updated 2026-09-12. Owner direction: apply the operational lessons from oMLX to existing cloud subscriptions first; defer local model serving. This is planned work, not a claim that the separate live dashboard integrations are packaged or production-qualified.

Optimize accepted useful work per constrained subscription unit, including coordinator and repair tokens. Idle capacity is a symptom, not the objective. Initial providers: Claude, Codex, ClinePass, OpenCode Go and Command Code GOAT, using each provider's permitted native interface. No assumed interchangeability between subscriptions and paid APIs.

## Priority and dependencies

| ID | Priority | Deliverable | Dependencies | Acceptance evidence |
| --- | --- | --- | --- | --- |
| CLOUD-01 | P0 — first | Supervised lifecycle: `grid start`, `stop`, `status`, `doctor`; durable configurable directories; collectors, uploader, scheduler and workers under one installation | Existing ledger and service qualification | Restart coordinator and collector, log out/lock screen where supported, and recover without a chat session, duplicate dispatch or temporary-directory dependencies; unsupported auth requirements are explicit |
| CLOUD-02 | P0 | Reliable quota collection and cooldowns per account/endpoint; freshness, source, observed time, next eligible read and failure reason | CLOUD-01 | Exercise 429 with numeric/date/missing/invalid Retry-After, expired auth, sleep/wake, stale/replayed snapshots and reset rollover; no retry before cooldown and no unknown-to-unlimited conversion |
| CLOUD-03 | P0 | Observable refresh request: authenticated request ID, queued/collecting/cooldown/completed/failed state and completion timestamp; distinguish downloading a snapshot from collecting new observations | CLOUD-01, CLOUD-02 | Phone request reaches local collector through outbound polling; concurrent clicks coalesce; offline Mac and provider cooldown are shown; refresh cannot launch inference, alter credentials or bypass limits |
| CLOUD-04 | P0 | Package and qualify each native cloud adapter with bounded execution, terminal records, cancellation and usage reconciliation | CLOUD-01, CLOUD-02; existing worker contract | One bounded representative job per provider plus timeout, truncated output, actual-model mismatch and ambiguous completion tests; capabilities unsupported by a provider are reported, not fabricated |
| CLOUD-05 | P1 | Versioned provider/model capability registry and onboarding diagnostics | CLOUD-02, CLOUD-04 | Exact model and account/plan mapping; context/tool/structured-output support, available limits and units, fallback policy, auth health, evidence source and expiry; changed or unqualified models cannot silently enter routing |
| CLOUD-06 | P1 | Unified activity and execution health dashboard | CLOUD-01, CLOUD-04 | Correlate task, attempt and native receipt; show queue/running/review/held state, last successful job, failure reasons, next action and observation age; process presence alone never proves useful progress |
| CLOUD-07 | P1 | Safe autonomous reconciliation, retries and backpressure | CLOUD-02, CLOUD-04, CLOUD-05 | Native evidence resolves held attempts; shared retry budgets, account concurrency caps, review-backlog limits and multi-project fairness survive restarts; uncertain execution never triggers a blind retry |
| CLOUD-08 | P1 | Persistent task/session affinity and bounded context reuse | CLOUD-04, CLOUD-05 | Resume compatible sessions without reclassification on each turn; preserve provider/model identity, stable prefixes where supported, context budget and checkpoint hashes; reauthorize model changes and measure cache effects only where reported |
| CLOUD-09 | P1 | Task-category evaluation and outcome-based routing | CLOUD-05, CLOUD-06, CLOUD-08 | Versioned representative tasks for extraction, documentation, tests, implementation and review; compare acceptance, latency, failure/rework, coordinator tokens and native subscription consumption against fixed routing; enable only measured improvements |
| LOCAL-01 | P2 — after cloud gates | Optional oMLX provider adapter | CLOUD-04, CLOUD-05, CLOUD-09 | Hardware fit and bounded local tasks demonstrated; compare accepted work and cloud tokens saved against memory, elapsed time and repair cost |
| LOCAL-02 | P2 | Local resource-aware admission and cache affinity | LOCAL-01 | Respect memory/concurrency limits and model availability; measure warm/cold performance; preserve cloud capacity and fairness while local work runs |

P0 exit: all five provider lanes are either qualified or explicitly blocked with actionable reasons, and one unattended service cycle survives restart with correct reservations and no hidden fallback. No local inference installation is required to reach this gate. P1 work on telemetry and evaluations may start alongside adapter qualification, but autonomous admission stays gated by the evidence above.

## Capability and cost contract

Separate account subscription windows from model token prices. Store plan allowance units, reset evidence, shared account aliases, concurrency, paid-overage settings and any API price separately. Unknown prices, quotas, reset times and token caps stay unknown. A supported model list is not evidence that a specific subscription pays for that model. Refresh capabilities from documented provider sources or qualified native discovery, with provenance and expiry; do not hardcode today's model ranking.

Quota-status request throttling and inference exhaustion are distinct conditions. A status endpoint's 429 pauses its collector; the dispatch decision still follows the explicit freshness/admission policy. Retry-After is a lower bound; conservative backoff applies when it is absent. Manual refresh never clears this bound. Authentication renewal uses the supported native flow, with no browser spoofing or silent paid fallback.

## Operational metrics

Persist queue age, running age, held age, review age, accepted tasks, failures, retries, rework, coordinator tokens, reported input/output/cache usage, native subscription consumption and quota observation age. Separate missing telemetry from zero. Completion and independent acceptance remain different events. Cloud snapshots export only authorized sanitized fields; remote operations require authentication and a bounded allowlist.

Evaluate affinity and routing over complete tasks/sessions, including reviewer and repair costs. No cross-provider token-price average for flat-rate subscriptions. Tune utilization only after correctness and accepted throughput improve.

## oMLX boundary

The inspiration is operational: managed lifecycle, discoverable models, explicit controls, persistent telemetry and repeatable benchmarking. Upstream reference: https://github.com/jundot/omlx . Integrate oMLX through a provider adapter when justified; let it own inference kernels, continuous batching, model loading and RAM/SSD KV caching. Grid owns project priorities, durable admission, account constraints, recovery and acceptance. Do not build GPU kernels, fork the inference engine or promise shared caches across different cloud models.

## Current implementation versus planned work

The core already provides durable admission, reservations, an outbox, bounded trusted workers and strict artifact receipts. The separately deployed capacity PWA has snapshot refresh feedback; Go has a scheduled collector and Claude has collector backoff. These are useful building blocks, not completion of CLOUD-01 through CLOUD-04. In particular, the phone Refresh button currently downloads the latest snapshot; CLOUD-03 adds an actual collection request lifecycle.

Delivery stages in SPEC.md remain qualification gates. This roadmap sets priority within and across them; it does not mark unfinished stages complete. Public publication and deployment follow the existing explicit authorization policy.
