# Evaluation record

Initial local evaluation: Python 3.12, SQLite write serialization, synthetic adapters. No provider inference is needed to run these tests.

Covered: shared account aliases; competing claims; cross-account workspace exclusion; all-window quota reservations; stale admission; immutable specs; duplicate and stale start; stale completion rejection; ambiguous hold; conservative quota debit; output/timeout/nonzero/invalid JSON; changed input manifest; artifact traversal/symlink/hash rejection; independent-family operator attestation; transactional outbox publish failure; priority routing; no implicit retry.

(Historical, superseded by B1 2026-09-17: the real-broker test was opt-in and skipped without `GRID_RUN_BROKER_TEST=1`, and the PostgreSQL version of the admission suite ran when `GRID_TEST_DATABASE_URL` named a disposable database. Both the broker path and its CI services were deleted; CI now runs the SQLite-backed suite only.)

This Mac had no PostgreSQL, RabbitMQ or Docker runtime during initial implementation. Therefore no claim is made that distributed crash recovery or Linux service integration has passed. Before production, complete SPEC stages 0.2–0.5, including worker-kill and broker-restart evaluations and native provider receipt reconciliation. Local tests now include two separately spawned processes competing through account aliases, and a process exiting after durable dispatch intent. These do not replace multi-host or broker-restart tests.

## Acceptance evidence

See `LOCAL_TEST_RESULTS.txt` for the actual local run summary. Provider contributions are described separately; their token reports do not demonstrate end-to-end operational savings.

Second pass: 34 passed, 1 real-broker test skipped. Process-lifecycle tests ran outside the shell sandbox because macOS denied process inspection within it. Child leaders remain unreaped until process-group cleanup; adapter stdin uses a file to avoid an unbounded pipe write.

Third pass: 38 passed, 1 skipped. Added stale/same-identity changed quota observation refusal, replay after completion preserving debits, and Windows-drive/NUL artifact-path rejection.

## Real-service qualification, September 12

Installed PostgreSQL 17.11 and RabbitMQ 4.3.5 locally. Real PostgreSQL regression suite: 39 passed, one broker-only test skipped. Separately, real RabbitMQ/Celery delivery and duplicate suppression: one passed. These were isolated loopback-only test services, not a deployment. Broker restart, actual Celery worker-kill/redelivery and Linux CI remain outstanding.

The first broker run failed because the source package was not on the subprocess import path; the invocation now supplies PYTHONPATH. The second exposed RabbitMQ 4.3 rejection of Celery's transient nonexclusive control/event queues. Explicit exclusive queues fixed that, and the real test passed.

Claude's bounded independent review completed after native OAuth refresh. Reproduced and fixed delayed quota observations restoring locally debited units: observations now preserve debits from completions after sampling. Its proposed PostgreSQL ON CONFLICT race was not adopted without reproduction. The manual abandoned-attempt helper still lacks native heartbeat evidence: it may conservatively hold a live attempt. It must not be scheduled as automatic expiry/release.

Cline qualification: Python 3.9 rejected nanosecond RFC3339 timestamps in the external quota collector. An isolated fix passed eight tests; missing reset times remain unknown. Installed thinking-none mapping omitted upstream reasoning settings. A bounded low-effort canary completed with verified model, 607 output tokens and one independently passing test. These external adapter fixes are qualified evidence, not yet integrated into this repository's adapter layer.

Alpha freeze: 56 PostgreSQL tests passed, three broker tests separately passed (delivery, killed worker, broker application restart). Linux CI configured, not executed. Clean wheel installation and offline demo passed. See RELEASE.md for scope.

## GitHub publication qualification

Candidate `0ed2b9b` passed [Linux CI](https://github.com/fordjam/inference-grid/actions/runs/34695750001), including dashboard HTTP/display/refresh tests, Docker build, SQLite and PostgreSQL suites, and the three real-broker tests including container restart. A subsequent local review found a capacity decrease after queue admission was not rechecked at dispatch; two regressions and a conservative hold fix bring the local suite to 61 passed, three broker-only skips. The repaired candidate must pass CI before tagging.

## Scorecard

| family | model | category | attempts | completed | accepted | acceptance rate | held | resolved | mean tokens in | mean tokens out | last attempt |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| glm | glm-5.3-flash | independent_review | 4 | 2 | 2 | 50% | 0 | 2 | 7883 | 12085 | 2026-09-12 20:24 |
| glm | glm-5.3-flash | pure_function | 1 | 1 | 1 | 100% | 0 | 0 | — | — | 2026-09-12 19:10 |
| glm | glm-5.3-flash | tests_multi_file | 5 | 2 | 2 | 40% | 0 | 3 | — | — | 2026-09-12 20:18 |
| glm | z-ai/glm-5.3-flash | tests_multi_file | 3 | 1 | 1 | 33% | 0 | 2 | 11214467 | 44454 | 2026-09-13 03:07 |
| kimi | kimi-k3 | independent_review | 24 | 21 | 16 | 67% | 0 | 3 | 331 | 59 | 2026-09-13 22:22 |
| kimi | kimi-k3 | unrecorded | 3 | 0 | 0 | 0% | 0 | 3 | — | — | 2026-09-13 17:28 |

Reading this: GLM-5.3-Flash on Go is 6/7 accepted for tightly specified pure functions at roughly 400 input / 1,500 output tokens each; the one miss was an output-format slip. GOAT (same model behind Command Code) completed a multi-file tool job at ~100k input tokens. The independent-review category is 0/4 across three model families, for three unrelated reasons that a capability registry must capture separately: hosting policy (`deepseek-v4-flash` needs a China-hosting opt-in the workspace disables), protocol (`grok-4.6` refuses the OpenAI-compatible endpoint), and reasoning allocation (`kimi-k3` spent its whole 6,000-token budget in a `reasoning` field and returned no content). None of these is a quality signal about the model; all are permission or budget facts to record before routing. An independent different-family review has since landed: Z.ai glm-5.3-flash (through Claude Code headless) rejected coordinator-written publication code with two demonstrable findings after two attempts lost to unbounded thinking; both findings were fixed. See CONTRIBUTIONS.md. No Grid attempt has reached the `accepted` state yet because coordinator-written code is not itself a Grid attempt.

Selection policy until evals exist: route pure functions to Go/GLM-5.3-Flash with the output format restated last; reserve GOAT for multi-file work; give reasoning models a review budget of at least 16k output tokens and verify `content` is non-empty before any verdict is read; record every provider/model/protocol refusal as a lane fact.

