# Dispatcher supervision

`inference-grid-service run --state-dir /absolute/private/state` runs scheduler and publication cycles in bounded child processes. Set GRID_DATABASE_URL and GRID_BROKER_URL in its environment and run `inference-grid init` first. Provider workers remain separate: this command does not start Celery, PostgreSQL, RabbitMQ or provider collectors.

Defaults: 15-second successful-cycle interval, 30-second total cycle deadline. `--interval` and `--cycle-timeout` accept positive seconds up to 3600. Failed cycles back off up to max(300 seconds, configured interval). The parent updates an atomic mode-0600 service.json heartbeat while running, idle and backing off. It records only counters/times and fixed error codes, never child logs or connection strings. A per-state-directory file lock prevents duplicate local supervisors using the same directory; this is not a cross-host singleton guarantee.

SIGTERM/SIGINT interrupt waits and stop the owned cycle process group. Inference workers are not killed. A broker timeout may be ambiguous; the next cycle republishes original attempt IDs from the durable outbox, and existing dispatch fencing prevents duplicate starts. Held attempts/reservations are never automatically released. Long queues may only partially progress within one cycle; batch fairness and a unified service manager remain planned.

Examples in examples/services provide systemd-user and launchd templates. Replace all placeholder paths/configuration locally. Store runtime credentials in private mode-0600 configuration; never commit populated templates. Install the package into the configured virtual environment and initialize the ledger before enabling the service. These templates are not an installer and have not been qualified across every host configuration. Use your OS service manager to stop/unload the service, not a stale PID read from its status file.

A healthy dispatcher heartbeat does not establish provider auth, active Celery workers, available quota or accepted output. Use doctor and attempt records for their respective diagnostics.
