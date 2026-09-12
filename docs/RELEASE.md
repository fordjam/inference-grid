# 0.1.0a4 experimental alpha

Adds the observable refresh request lifecycle for the hosted capacity dashboard (queued/collecting/completed/cooldown/failed with expiry and coalescing), a Mac-side outbound refresh agent that runs one bounded trusted collector command, a strict provider observation normalizer gating `collect()` HTTP 200 payloads, and a native Command Code (GOAT) outcome normalizer. Both normalizers were authored by OpenCode Go through real Grid attempts and accepted after locally written tests passed unmodified.

Upgrade: no ledger schema change since a3; run `inference-grid init` if upgrading from earlier. The hosted dashboard adds its `refresh_request` table on start. The refresh agent needs its own long-lived process (see `examples/services/inference-grid-refresh-agent.plist`); without `collect_argv` it reports snapshot-only completions.

Boundaries: refresh requests cannot launch inference, change credentials or shorten cooldowns; the collector command owns cooldown enforcement. Normalized observations are returned, not yet published into account capacity. GOAT's session-scoped effort override was demonstrated by one canary; its trusted adapter and a representative job remain unqualified. Cline's lane is limited by weekly usage and needs adapter changes before another attempt. See CONTRIBUTIONS.md for attempt evidence, including held attempts.

The GitHub release records exact validation, CI and artifacts. Previous alpha evidence remains in prior release notes and EVALUATION.md.
