# 0.1.0a2 experimental alpha

This release adds durable account/endpoint cooldowns, read-only doctor diagnostics, conservative Cline outcome classification and bounded structured JSON decoding. It includes the cloud-first roadmap and capacity dashboard fixes from main after a1.

Before upgrading an existing ledger, run `inference-grid init` to add the cooldown table. No automatic migration deletes or releases existing work. Full native provider adapters and supervised service installation remain unfinished.

Local tests and independent review cover maximum deadline persistence, account aliases, quota-refresh independence, usage/inference separation, queued-to-held behavior, already-running work, expiry without reservation release and ordered cooldown/start races. CI repeats core tests on PostgreSQL and exercises actual broker recovery, wheel installation and dashboard/container checks. See the GitHub release for exact candidate CI evidence.

No full autonomous-loop, native-provider review approval, security sandbox or exactly-once external execution guarantee is claimed. A completed response remains distinct from an accepted artifact. Go tasks were exercised through Grid; one completed and needed local corrections, another was correctly held for malformed transport formatting. No blind retry was used.

Wheel and source archives are attached to the GitHub prerelease; PyPI publication is not included. Prior a1 evidence remains in EVALUATION.md and the a1 release notes.
