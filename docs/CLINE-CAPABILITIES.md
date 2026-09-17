# Cline reasoning capabilities

**Retired 2026-09-16** along with the `cline_cli` lane kind (see `docs/LANES.md`, "Cline
(retired 2026-09-16)"): the lane module this qualified is deleted. The rest of this document
is kept as the record of the qualification work described below.

Qualification applies to CLI3.0.61 and the exact subscription model `cline-pass/qwen3.8-max`; requalify new versions or model catalogs.

The CLI's `--thinking none` maps to `thinking=false` and clears the effort. The installed handler emits no reasoning wire field in this case; it does not establish server-side reasoning disable. `--thinking low` maps to `reasoning_effort=low` in an offline handler request capture. The model advertises minimal/low/medium/high/xhigh, but this CLI flag parser does not accept minimal. Low is the lowest directly expressible supported value.

Use explicit capability states: requested setting, observed local wire mapping, and server compliance (unknown unless independently observable). Refuse tasks requiring guaranteed reasoning disabled on this configuration. Do not label omitted settings as disabled or infer token ceilings from wall-clock cancellation.

One bounded low canary produced a verified native completion and passing tiny test. This is evidence of usability, not a reliability benchmark. Native `--retries` limits consecutive mistakes, not inference requests. Output cancellation is reactive; native hard output-token caps remain unqualified.

Quota timestamps may contain nanoseconds. Floor precision to microseconds rather than rejecting or rounding deadlines later. A missing reset can remain unknown alongside fresh measured usage; it must never imply replenishment. All required utilization windows and policy freshness thresholds still apply.
