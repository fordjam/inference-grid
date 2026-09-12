# Cloud hardening: first operational slices

## Read-only diagnostics

Run `inference-grid doctor` with the same `GRID_DATABASE_URL` as your workers, or pass `--database` before the command. Exit 0 means only the reported ledger/executable checks passed; exit 1 means attention is needed. The command never initializes a missing database, starts work, authenticates providers, releases held reservations or repairs the broker. Database URLs, adapter arguments and exception text are not printed.

Checks include missing schema, unconfigured/stale accounts, held attempts and missing executables. Relative executable paths are unresolved because the worker runs inside a new attempt directory. Authentication and broker health remain explicitly not checked. A missing provider lane cannot be inferred from the configured-account list; the capability registry is still planned.

## Retry-After policy

`retry_deadline(value, now, fallback_seconds)` returns an absolute deadline from an HTTP seconds/date header or a positive fallback. It refuses nonfinite/overflowed deadlines rather than shortening them. `cooldown_key(account, endpoint)` keeps usage-read cooldowns distinct from inference cooldowns.

This is a pure parsing contract, not a retry engine. Callers must persist the maximum existing/new deadline atomically, enforce attempt budgets, honor manual-refresh cooldowns and reject replay when prior execution is uncertain. A valid zero/past header permits now at the parser level; collector policy may still impose conservative backoff.

## Native terminal classification

See CLINE-OUTCOMES.md. Controller-owned cancellation wins over a native success event; missing/empty/wrong-model/trailing terminal records remain unqualified. No parser verdict accepts an artifact or authorizes another call.

## Actual Grid exercise

A tiny native OpenCode Go task ran through Grid's configure/submit/claim/execute path using a private trusted adapter wrapper. Exact native model and stop reason were verified, with 205 input and 373 output tokens reported. Grid recorded completed, not accepted. The immutable original output failed an overflow test; explicitly local corrections produced the integrated derivative and ten passing policy tests. No second provider call was made. Native credentials, runner configuration and raw receipts are not part of this repository.

Cline normalization was implemented locally using synthetic tests and captured-run replay checks; no new native Cline inference was spent. GOAT's installed reasoning setting writes global configuration and remains incompatible with the tested isolation policy; it was held rather than rerun with ineffective defaults. No claim of end-to-end qualification for these adapters is made.

The next integration is durable collector cooldown state and observable collection requests, followed by a supervised service lifecycle. These slices do not complete CLOUD-01, CLOUD-02 or CLOUD-04.
