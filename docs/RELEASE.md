# 0.1.0a1 public-alpha candidate

Suitable for sharing as an experimental, trusted-operator project. Not a production service or a complete autonomous coding platform.

## Completed release checks

- Current candidate: 61 regression tests passed locally (three opt-in broker tests skipped). Earlier core qualification: 56 tests passed against PostgreSQL 17.11 on macOS.
- Three RabbitMQ 4.3.5/Celery 5.6.3 tests passed: real delivery, killed worker after adapter start, queued job surviving broker application stop/start.
- Capacity checks: nine HTTP tests plus display and refresh behavior tests passed. CI additionally builds the standalone Docker image.
- Wheel and source archive built; clean wheel installation, CLI help and offline demo worked.
- Packaging includes the standalone capacity deployment, examples, docs, Compose and security guidance in the source archive. License and README metadata are present.
- Source-tree scan found no user-specific paths, account credentials or private project data in the release files. This is not a comprehensive security audit.

## Explicit limitations

- Native quota collectors and end-to-end Go/Cline/Command Code adapters are not bundled. Pure quota and native receipt validators are available; external canaries only qualify the external invocation configurations.
- No auth/RBAC, untrusted-code sandbox, automatic held-attempt reconciliation, multi-host fairness or migrations beyond additive initialization.
- Operator-attested review is not authenticated reviewer identity.
- macOS broker application restart was tested, not a whole-host power loss. Linux CI passed on GitHub for candidate `0ed2b9b`, including container restart. The concurrency-limit repair is rerun before tagging.
- No claim of exactly-once external inference or hard provider token limits.

## Publication procedure

Review the source diff and scope, create the intended GitHub repository with explicit visibility, push the alpha branch and run its Linux CI. Fix any platform failures before tagging or advertising platform qualification. The repository is public at https://github.com/fordjam/inference-grid . PyPI publication is not part of this release. A stable release requires the remaining SPEC gates.

A separate local review reproduced and repaired capacity shrink between claim and dispatch. Both queued-only and already-running cases are covered; reservations remain held for reconciliation. The four native publication-review attempts did not return usable reviews; see CONTRIBUTIONS.md.
