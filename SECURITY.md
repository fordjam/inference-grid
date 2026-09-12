# Security boundary

Do not expose the CLI or database to untrusted callers. v0.1 has no authentication, RBAC or multi-tenant isolation. Its `authorized` and reviewer fields are local operator attestations, not identities. Trusted database operators and adapters can forge receipts; hashes detect changed content, not authorship.

Adapters execute local code. Separate working directories and process-group cleanup are not a sandbox. Apply OS/container filesystem, network, CPU, memory and storage limits before running untrusted code. A background process can escape a process group. Captured output bounds are checked periodically, so transient writes can exceed the threshold.

Credentials must be resolved by a trusted host adapter; never include them in task specs, arguments, broker messages, receipts or source control. The worker intentionally does not forward HOME or arbitrary environment variables. A native provider adapter must explicitly use a secure credential mechanism and verify it works while the desktop is locked.

Compose binds services to loopback for development. Use TLS, unique credentials, network access controls, durable backups and least-privilege roles for real deployments. Do not use example credentials outside local tests. Do not publish native account logs or proprietary task payloads.
