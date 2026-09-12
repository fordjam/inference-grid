# Implementation contribution record

This project used bounded external-provider tasks to reduce coordinator implementation work. Outputs were reviewed locally; a provider completion was not treated as acceptance.

| Provider | Bounded assignment | Observed result |
| --- | --- | --- |
| OpenCode Go / GLM 5.3 Flash | Pure alias resolver and five tests | Integrated after review; 160 input / 814 output tokens reported |
| Cline | Receipt validator and tests | Output guard stopped attempt before files were delivered; no retry; implemented locally |
| Command Code GOAT / GLM 5.3 Flash | Short failure-policy draft | Draft delivered; unsupported execution guarantees corrected before inclusion; 35,276 input / 2,018 output tokens and 11,200 cache-read tokens reported separately |

GOAT's small task still incurred substantial input overhead. These results do not justify routing every small task externally. Prefer Go for tightly bounded pure functions while collecting more evidence; use Cline only after its reasoning/output behavior is bounded at the native adapter. Do not infer dollar savings from token counts across subscriptions. Native account records remain outside the public source tree.

Public-alpha follow-up: Cline delivered native receipt structural checks plus five independently passing tests (2,176 output tokens). Go's quota implementation hit a 2,500-token cap and was rejected; a local coordinator completed the portable module with twelve boundary tests. GOAT reviewed release metadata and packaging. These contributions do not establish production provider adapters.
