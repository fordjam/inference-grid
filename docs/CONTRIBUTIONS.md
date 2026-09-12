# Implementation contribution record

This project used bounded external-provider tasks to reduce coordinator implementation work. Outputs were reviewed locally; a provider completion was not treated as acceptance.

| Provider | Bounded assignment | Observed result |
| --- | --- | --- |
| OpenCode Go / GLM 5.3 Flash | Pure alias resolver and five tests | Integrated after review; 160 input / 814 output tokens reported |
| Cline | Receipt validator and tests | Output guard stopped attempt before files were delivered; no retry; implemented locally |
| Command Code GOAT / GLM 5.3 Flash | Short failure-policy draft | Draft delivered; unsupported execution guarantees corrected before inclusion; 35,276 input / 2,018 output tokens and 11,200 cache-read tokens reported separately |

GOAT's small task still incurred substantial input overhead. These results do not justify routing every small task externally. Prefer Go for tightly bounded pure functions while collecting more evidence; use Cline only after its reasoning/output behavior is bounded at the native adapter. Do not infer dollar savings from token counts across subscriptions. Native account records remain outside the public source tree.

Public-alpha follow-up: Cline delivered native receipt structural checks plus five independently passing tests (2,176 output tokens). Go's quota implementation hit a 2,500-token cap and was rejected; a local coordinator completed the portable module with twelve boundary tests. GOAT reviewed release metadata and packaging. These contributions do not establish production provider adapters.

## Publication review round, 2026-09-12

The following native attempts were dispatched but did **not** deliver accepted reviews:

| Provider | Scope | Outcome |
| --- | --- | --- |
| OpenCode Go | Quickstart/documentation packet | Output limit reached; no final answer |
| Cline | Packaging/CLI packet | Captured-output budget reached; no completed review |
| Command Code GOAT | Dashboard regressions | 120-second timeout; no report |
| Claude / Opus requested | Ledger, queue and worker review | 180-second timeout; no terminal model confirmation or report |

No automatic retries or fallback were used. Local coordinator checks found and fixed missing deployment files in the source archive and misleading quickstart environment instructions. These fixes must not be credited to a successful native review. Future qualification must measure reasoning/output allocation and retain bounded native progress events; repeatedly increasing wall-clock limits is not evidence of a usable adapter.

A separate local Codex review of ledger/queue/worker reproduced the capacity-shrink dispatch defect. The fix and two regression cases were independently checked; no other blocker was found within that limited trusted-operator review scope. This is not an Opus or Go approval.

## Post-a3 distribution round, 2026-09-12 (coordinator: Claude Code after Codex credit exhaustion)

| Provider | Bounded assignment | Grid evidence | Outcome |
| --- | --- | --- | --- |
| OpenCode Go / GLM 5.3 Flash | Strict `normalize_observation(raw, now)` provider observation normalizer (allowlisted provider/window ids, aware timestamps, future tolerance, None-preserving usage, ordered sanitized output) | Task `observation-normalizer-1`, attempt `66423216…`, receipt completed/stop/actual model `glm-5.3-flash`; 317 input / 1,762 output tokens reported | Accepted: 4 locally authored unittest cases (23 boundary/refusal assertions) passed with no code repairs; integrated as `inference_grid.observation` and wired into `collect()`'s validate branch. Deviation: 52 lines against a requested 40, cosmetic |

Learning: Go handled a tightly specified pure validator with a complete ruleset on the first attempt, as it did for the status classifier. The brief's line budget was ignored while every behavioral rule held; treat size hints as advisory and test behavior instead. Tests were written locally before dispatch so acceptance did not depend on provider-authored tests.
