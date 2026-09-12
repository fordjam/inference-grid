# Failure Policy

Every remote interaction can fail. This scheduler recovers only through safe, explicit actions and never assumes a dispatch "probably" did not happen.

## Failure Handling

| Failure | Safe action |
| --- | --- |
| Confirmed predispatch connection refusal | Dispatch was never sent; retry is safe if replay is proven safe |
| HTTP 429 | Stop dispatching; honor `Retry-After` and recheck fresh quota before resuming |
| Authentication failure | Pause the worker; no retries; resume only after credentials are repaired |
| Timeout after possible provider acceptance | Ambiguous: retain the reservation and hold for reconciliation; never relaunch blindly |
| Partial / truncated output | Retain the reservation; discard unusable output only per replay-safety rules; reconcile before redelivery |
| Worker loss | Retain the reservation and hold for reconciliation rather than launching a duplicate |
| Stale fencing generation | Reject the completion outright; the current generation owns the work |

## Principles

**Replay safety.** Retry only when replay is proven safe — the dispatch is idempotent or provably never reached the provider. A confirmed predispatch refusal still requires fresh admission and remaining retry budget.

**Rate limits.** On HTTP 429, dispatch stops, `Retry-After` is honored, and current quota is re-read before resuming. Quota state is refreshed, never assumed.

**Authentication.** Auth failures pause the affected lane immediately. Retrying against broken credentials only burns budget and risks lockouts.

**Ambiguity.** A timeout after possible provider acceptance and worker loss are ambiguous: the provider may already hold accepted work. The scheduler retains the reservation and holds for reconciliation instead of launching a blind duplicate. State is resolved before any relaunch.

**Fencing.** Completions carry a generation token. A stale completion is rejected — work owned by an earlier generation cannot write results.

**No assumed acceptance.** A native "completed" signal from the provider is not artifact acceptance. Artifacts count only after verification and registration.

**Accounting separation.** Currency, tokens, and subscription units remain separate ledgers. They are never merged, implicitly converted, or double-counted.

**Dispatch budget.** One shared, bounded dispatch budget covers all dispatch attempts, and adapter/gateway retries count against it. No lane can hide retries from the budget; when it is exhausted, dispatching halts for reconciliation.

**No exactly-once.** Durable dispatch intent suppresses repeated local starts. Fencing rejects stale local acceptance, but does not prevent every duplicate external inference. Native reconciliation remains necessary.

## Implementation status

The table is the target policy. v0.1 conservatively holds all unsuccessful adapter outcomes and has no automatic retry or native 429/auth classifier. Known-terminal failures can be resolved by a future audited reconciliation action. Neither this draft nor a provider response proves a guarantee.
