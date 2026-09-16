# Kimi-K3 lane report — packet L6: `cline-http` passes its canary, and the refusal was the model id namespace (2026-09-16)

Lane: Kimi-K3 (build lane). Base: `6c23c6b` (the tip of `glm/work` at branch time).
Branch: `packet/packet-l6`, one commit, not pushed.

## Why

The `cline-http` lane — ClinePass over the Go-style HTTP adapter (`lanes/go.py`),
lane model `z-ai/glm-5.3-flash` — never passed `canary-cline-http`. The live board
recorded why the first run ended (`grid/board/canary-cline-http.json`):

> `superseded: canary-cline-http-2 — glm-5.3-flash free-tier daily cap (HTTP 429)
> reset overnight; re-run 2026-09-16`

`go.py` has no 429 special case, so the held attempt's refusal was the generic
`endpoint returned HTTP 429`. The canary was the only task on the lane and no other
task ever ran on it, so that 429 is the whole history of the refusal.

## Finding the refusal — endpoint, headers, model id

The brief names three suspects; the 429 answers two of them by itself:

- **Endpoint** — the per-provider switch (`go.py::endpoint_for`, brief 14 G1) already
  sends `clinepass` to `https://api.cline.bot/v1/chat/completions`. A wrong path
  would be a 404, not a post-auth quota answer. Not the endpoint.
- **Headers** — a 429 is answered *after* authentication; a wrong `Authorization`
  shape would be a 401/403, and the G1 packet's raw-API observations (a real answer
  for this model, 402s for `kimi-k3` and `deepseek-v4-flash`) were taken with the
  same key. Not the headers.
- **Model id namespace** — `run` put `request["model"]` on the wire verbatim:
  `z-ai/glm-5.3-flash`, a vendor id. ClinePass bills vendor ids to pay-as-you-go
  credits and draws on the subscription only for its own `cline-pass/<model>`
  namespace (the "pass models are the `cline-pass/` id namespace" rule already
  written in `docs/LANES.md`). A vendor id it chooses to serve anyway comes from a
  free tier with a daily cap — which is exactly what "free-tier daily cap (HTTP
  429)" names. **This is the refusal.**

Why a vendor id `answered` for G1 but 429'd for the canary: both were the free
tier. A one-off probe fits under the daily cap; the canary's first run exhausted
it, and the task was superseded to re-run after the overnight reset — a quota
artefact, not a fix, which is why `canary-cline-http-2` was dispatched.
## What landed

**`src/inference_grid/lanes/go.py`**, three small pieces beside the existing
provider switch:

- **`PASS_NAMESPACES = {"clinepass": "cline-pass/"}` and `wire_model(provider,
  model)`** — the wire carries `cline-pass/<bare id>` for a clinepass lane
  (idempotent for an already-namespaced id, prefix-stripping for a vendor id), and
  the lane's own id verbatim for every other provider.
- **`run`** sends `wire_model(provider, request["model"])` in the request body and
  records `verdict["wire_model"]` whenever it differs from the lane's id, so a hold
  names what was actually asked for. The receipt keeps `actual_model` as the lane's
  canonical id — the runner's model match (`request["model"] == lane["model"]`) and
  the scorecard key on the lane record, not on an endpoint alias.
- **`qualify(response, model, aliases=())`** — the endpoint may echo either id in
  its response (the pass id it was sent, or the vendor id the lane carries; G1
  observed the vendor form). Both name the one model, so `run` passes the lane id
  as the one accepted alias — the same "one model behind two ids" rule the runner's
  `bare_model` already encodes for the scorecard. Any *other* model is still
  `request served by an unexpected model`, and `aliases` defaults to nothing: the
  opencode path and every existing caller see byte-identical behaviour.

**`tests/test_lane_go.py`**, five tests (all offline, fake `send`):

- `test_the_cline_http_canary_passes_on_the_pass_namespaced_wire_id` — the canary
  itself: brief "Reply with exactly OK", artifact `reply.txt`, lane model
  `z-ai/glm-5.3-flash`, provider `clinepass`, against the recorded response shape
  (`{"provider": "clinepass", "data": {<chat-completions document echoing
  cline-pass/glm-5.3-flash>, stop, "OK"}}`). Asserts the completed receipt, the
  artifact's content, the wire id, `verdict["wire_model"]`, the recorded provider,
  and that `native.json` preserves the endpoint's own wrapped document.
- `test_429_is_the_free_tier_cap_the_first_canary_hit` — pins the recorded refusal:
  HTTP 429 → `endpoint returned HTTP 429`, the response body never in the verdict,
  no `native.json`.
- `test_clinepass_vendor_id_echo_still_qualifies` and
  `test_clinepass_another_model_behind_the_prefix_is_still_refused` — the alias
  accepts the one model's two ids and nothing else (`cline-pass/kimi-k3` behind the
  prefix still refuses).
- `test_wire_model_maps_clinepass_ids_into_the_pass_namespace` — the mapping
  itself, including idempotence and the untouched opencode pass-through.

**`docs/LANES.md`** — the `cline-http` section gains a **Model id namespace (L6)**
paragraph: the wire id, why the lane record keeps the vendor id, the
`wire_model` verdict field, the echo alias, and the 429 as the first canary's
recorded evidence. The plan-coverage paragraph is corrected to match the evidence
(the 429 and both 402s all came from the vendor id space; the pass namespace is
what the subscription serves). The lane is **not** marked `explicit_only`: the
endpoint demonstrably serves chat completions for this model — G1 observed a real
answer and the recorded refusal is a quota answer, not a capability refusal.

## Boundaries worth the operator's eye

- The lane record keeps `z-ai/glm-5.3-flash` deliberately. Qualification rows are
  per bare model, so `glm-5.3-flash` evidence the `cline_cli` lane earned already
  counts; what the `cline-http` canary proves is the harness — which is precisely
  what this fix repairs. If the operator would rather the record itself carry the
  pass id, that is a `lanes.json` rename away, but then the runner's model match
  and every scorecard row move with it.
- `cline-pass/` namespaces every clinepass lane the same way, including a future
  `cline-http` kimi/deepseek lane: those models bill credits as vendor ids
  (`model_not_in_plan`) and the pass id is the only subscription-priced path — the
  same fix applies, but each still earns its own canary before work.
- The 429 needs no special classification: unlike a 402 (a lane-config fact, never
  retried), an exhausted daily cap is transient in exactly the way the board
  already expresses by superseding the task, and the generic `endpoint returned
  HTTP 429` refusal records all the information the driver has.

