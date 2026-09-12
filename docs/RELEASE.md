# 0.1.0a3 experimental alpha

Adds bounded dispatcher supervision, heartbeat/backoff and manual OS-service templates, plus single-flight quota collection admission and a Go-authored HTTP status classifier.

Upgrade: run `inference-grid init` before restarting work; it adds collection_claims without releasing existing reservations. The new supervisor runs tick/publish only. Celery workers, data services and native collectors remain separate.

Provider callbacks must enforce network deadlines. The collector foundation does not publish observations, automatically clear orphaned claims or install existing dashboard feeds. See SERVICE.md and COLLECTION.md for boundaries. No full autonomous-loop, security sandbox or exactly-once external execution guarantee is claimed.

The GitHub release records exact validation, CI and artifacts. Previous alpha evidence remains in prior release notes and EVALUATION.md.
