# GLM lane report — brief 14, G1: the Go lane kind pointed at ClinePass (2026-09-15)

Lane: GLM-5.3-Flash. Base: `origin/glm/work`; branch `glm/g1-cline-http-the-go-lane-kind-pointed-at-c`,
one new commit on top of the operator's `829e95b` (which carried the packet spec in
`docs/handoff-glm-15.md`, not packet code), not pushed. The driver names this row and
report "brief 14" even though the packet is G1 of `docs/handoff-glm-15.md` Phase G —
they follow the driver's naming, as E2's report did.

## What landed (the packet)

`src/inference_grid/lanes/go.py` — the chat-completions endpoint is now a property of
the lane's `provider`; `lanes/config.py` is provider-authored and untouched:

- `ENDPOINTS` maps `opencode` → `https://opencode.ai/zen/go/v1/chat/completions` and
  `clinepass` → `https://api.cline.bot/api/v1/chat/completions`; `ENDPOINT` stays the Go
  URL (the default provider), so every pre-existing lane behaves byte-identically.
- `endpoint_for(provider)` resolves the URL; `run()` resolves it **before** reading the
  credential file, so an unknown `provider` refuses (`unknown provider: <name>`) before
  credentials are read or a request is built — testable with no credential file and a
  `send` that fails the test if called.
- `http_send(body, key, session, timeout, endpoint=ENDPOINT)` posts to the resolved
  endpoint; the injected-`send` contract is unchanged.
- `unwrap_data(response)` — ClinePass wraps the chat-completions document as
  `{"data": {...}}`. When a dict response carries no `choices` but a dict `data`, the
  inner document is what qualification, usage and finish-reason read; `native.json`
  preserves the endpoint's **own** document, wrapper included. The response's
  `provider` field (wrapper first, inner document second) is recorded in
  `verdict["provider"]` (None for an un-wrapped Go response).
- A `402` — ClinePass bills models outside the subscription plan to pay-as-you-go
  credits (`402 insufficient_credits`, observed 2026-09-15 on `kimi-k3` and
  `deepseek-v4-flash-0731`) — is classified as `refusal: model_not_in_plan` and never
  retried, the way a hosting-opt-in 403 is its own refusal; the error body is still
  never recorded.

`docs/LANES.md` — the `cline-http` lane entry (provider `clinepass`, family `glm`,
model `z-ai/glm-5.3-flash`, kind `go_http`, categories `independent_review`,
`pure_function`, key path only), what the module does differently for `clinepass`, and
the plan-coverage note above. The `go_http` row in "Packaged kinds" now says the
endpoint varies by provider.

Credential shape, deliberately unchanged: `read_key` keeps parsing the
`{"opencode-go": {"key": …}}` envelope whatever subscription the file opens. A
per-provider envelope would touch the refusal strings three existing tests assert;
the packet did not ask for it and the operator owns the file. Flagged here as the one
cosmetic rough edge (a `clinepass` credential file carries an `opencode-go` key).

## Tests

`tests/test_lane_go.py`, all offline against an injected `send`:

- `test_clinepass_wrapped_response_is_unwrapped_and_provider_recorded` — the wrapped
  shape completes, `verdict["provider"]` is `clinepass`, usage/finish-reason come from
  the unwrapped document, `native.json` keeps the wrapper.
- `test_402_names_a_model_outside_the_subscription_plan` — refusal `model_not_in_plan`,
  no `native.json`, error body never recorded.
- `ProviderEndpointTests.test_endpoint_follows_the_lane_provider` — the per-provider
  mapping, and the default (no provider key) stays on the Go endpoint.
- `ProviderEndpointTests.test_an_unknown_provider_refuses_before_any_request_or_credential`
  — unknown provider refuses with no credential file present and a `send` that raises
  if ever called.
- `test_http_error_records_status_only` moved from 402 to 500 — the packet changes the
  402 contract, so the bare-status form is now exercised with a code that has no special
  classification. Every other existing Go test is untouched and unchanged.

## The two failing gates, and what was done about them

**Pytest gate — root cause outside the packet, fixed at the test's own seam.** The
failing node was
`tests/test_lane_cline.py::ClineLaneTests::test_key_never_written_under_attempt`, which
asserted `"CLINE_API_KEY" not in os.environ` after a lane run. Two facts collided:

1. The driver's own gate environment injects `CLINE_API_KEY` for cline-adapter lanes
   (`scripts/run_lane.py` ~line 203: `env["CLINE_API_KEY"] = cline_key`), so the
   pytest subprocess the gate spawns starts with the key present. The lane code is
   byte-identical between HEAD and `origin/glm/work`; this is not a regression in the
   packet or the lane.
2. The gate's baseline cache is a single JSON per base commit
   (`~/.grid-workspaces/packets/pytest-baseline-e71c1ca….json`), shared by concurrent
   lanes. A lane running with a different environment overwrote it to `[]`, so the
   failure this round was classified `new` instead of `inherited`.

Per the standing rule (never weaken/skip/delete a test to pass a gate), the fix keeps
the test's promise and removes its environment assumption: it now snapshots
`os.environ.get("CLINE_API_KEY")` in `setUp` and asserts the run leaves it
**byte-identical**. That is strictly stronger than the old assertion in the clean case
(a lane that leaks its own key where none existed still fails, `None != "sk-…"`), and
it is deterministic under the driver's gate env. The two real defects behind the flip —
the driver injecting `CLINE_API_KEY` into the gate env, and the per-sha baseline cache
being overwritten by whichever lane runs first — live in the driver's running code and
`gates.py`, outside this packet; both are named here rather than patched.

**Commit gate — resolved by this commit.** The branch was zero commits ahead of
`origin/glm/work` (HEAD `829e95b` is the operator's spec commit, authored upstream, with
a Claude Opus 5 trailer). This report, the CONTRIBUTIONS row and the packet code form
the required single new commit with the driver's trailer.

## Final gate (local reproduction, with its limits)

- `tests/test_lane_go.py`: 23 passed, 2 failed locally — the 2 (`ReplyModeTests`,
  `ToolMarkupTests`) are the pre-existing `/private/tmp` `PermissionError` of this bot's
  sandbox (they hardcode that base and pass in the gate's unsandboxed run); all six new
  and touched go-lane tests pass.
- Full suite: this bot's own sandbox denies nested `sandbox-exec`, so the ~80
  pre-existing macOS-sandbox tests (`killpg`/`/bin/ps` families, including
  `test_lane_cline.py`) fail here exactly as they did before any change; the sorted
  failing set outside those families is empty. The gate's own run, with the real
  sandbox, is the judge; on the branch the cline key test now passes under **both**
  environments (key present or absent), so the pytest gate no longer depends on which
  lane wrote the baseline cache last.
- `ruff format` and `ruff check` on the changed files: clean.

## Defects found in existing code

- `scripts/run_lane.py` injects `CLINE_API_KEY` into the environment the gates inherit
  (named above, not patched — driver-owned and running).
- `gates.py`'s baseline cache is a bare per-sha file shared by concurrent lanes; a
  concurrent write flips `inherited` ↔ `new` classifications (named above, not patched).
- Cosmetic: `read_key`'s `opencode-go` envelope is subscription-agnostic in practice
  (see above).
