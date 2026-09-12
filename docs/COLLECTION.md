# Cooldown-aware collection

`collect(ledger, alias, collector, timeout_seconds=30, fallback_seconds=60)` admits one trusted quota collector at a time per canonical account. Ledger.initialize (CLI init) adds collection_claims. Existing usage cooldowns prevent calling the collector; aliases share the claim. The callback runs outside the database transaction with an absolute deadline argument.

The callback MUST enforce its own network timeout. This API is synchronous and cannot kill a hung callback. A claim that survives a crashed process is not automatically reused after expiry: it requires explicit operator reconciliation. No force/clear/retry API is provided yet. Doctor reports expired claims.

Return CollectionResponse(status=..., observation=..., retry_after=..., received_at=...). HTTP429 persists the maximum usage deadline; success does not clear an existing cooldown. Unrepresentable deadlines, invalid 429 timestamps and persistence uncertainty retain the claim. Auth and error statuses are sanitized; raw bodies and exception strings are not returned. HTTP200 only reports response_ok_unvalidated (or the supplied unknown/error state); observations remain None until a separate trusted provider normalizer/publication integration is supplied. No quota values are inferred from a successful HTTP status.

The Go-authored quota_status classifier was delivered via an actual Grid attempt and passed six tests without code repairs (139 input / 163 output tokens). The runner and subsequent hardening were implemented and reviewed locally. Native runner credentials, raw logs and provider-specific capture state remain outside the public repository.

This is a reusable collector admission foundation, not a claim that the existing phone dashboard feeds are automatically migrated. Next: explicit provider normalizers, collector installation, observable collection requests and safe recovery of orphaned claims.
