# Cline outcome normalization foundation

**Retired 2026-09-16** along with the `cline_cli` lane kind (see `docs/LANES.md`, "Cline
(retired 2026-09-16)"): `cline_outcomes.py` and `tests/test_cline_outcomes.py` are deleted.
The rest of this document is kept as the record of the qualification work described below.

Local implementation for CLOUD-04, not a new native dispatch or complete Cline integration. `classify_cline(events, supervisor, expected_model)` is pure: it returns `native_complete`, `interrupted` or `unqualified`, a fixed reason, sanitized progress counters, and a structural receipt only for qualified native completion. It never changes account capacity, retries work, or accepts artifacts.

Trust boundary: supervisor metadata must come from the controller, outside model-writable files. Input must be the complete parsed JSONL event stream; reject malformed/truncated JSON before classification. The model must not be allowed to author its own supervisor receipt. Captured text is retained only in the optional successful receipt; avoid logging that receipt. Progress contains no prompt or text.

A stop imposed by output/iteration/log/runtime supervision overrides a native success record. Normal exit requires exactly one terminal run_result, as the last event, exact requested model and provider, finishReason=completed and nonempty text. Unknown/trailing terminal shapes fail closed pending adapter qualification. Exit0 alone and a completed record with empty text are insufficient. The conservative last-event rule may reject future CLI versions that append telemetry; qualify those shapes explicitly instead of dropping events.

Progress counts observed iteration_start events and the largest valid cumulative output-token report. Missing token telemetry stays null; delayed totals may substantially undercount an in-flight generation. Neither token telemetry nor totalCost0 establishes free subscription consumption. Existing character/iteration cancellation is reactive, not a hard provider token ceiling.

Before a Grid worker can claim task completion it must separately verify attempt/manifest identity, declared artifact contents and hashes, workspace boundaries and its adapter contract. `native_complete` is not that gate. Interrupted/unqualified dispatches must retain ambiguous-provider handling; this helper does not infer safe replay or stopped upstream inference.

Validation:10 pure unittest cases, plus three local evidence replays recorded privately (raw provider evidence is not bundled). Public fixtures contain only synthetic model/text and no credentials or private logs. Replay summary intentionally omits terminal text.

Run: `python -m pytest tests/test_cline_outcomes.py`.

Integration: place cline_outcomes.py in the package; update test import to `from inference_grid.cline_outcomes import classify_cline`. This helper is included in the package; a complete native dispatch bridge remains unqualified.
