"""Durable single-flight quota collection; no provider credentials or quota inference."""

import math
import time
import uuid
from dataclasses import dataclass

from sqlalchemy import Column, Float, MetaData, String, Table, select, update
from .ledger import Refused, accounts, aliases, cooldowns
from .retry_after import retry_deadline
from .quota_status import classify_quota_status

_meta = MetaData()
collection_claims = Table(
    "collection_claims",
    _meta,
    Column("account", String, primary_key=True),
    Column("token", String),
    Column("deadline", Float, nullable=False),
    Column("last_status", String),
)


@dataclass(frozen=True)
class CollectionResponse:
    status: int
    observation: object = None
    retry_after: object = None
    received_at: object = None


def initialize_collections(ledger):
    _meta.create_all(ledger.engine)


def _number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def collect(ledger, alias, collector, *, timeout_seconds=30, fallback_seconds=60):
    """Call trusted collector(deadline=UnixTimestamp) once, after durable admission.

    Collector owns transport timeout and normalized observations. Its raw result is
    NOT published here: callers receive status only and a successful observation
    only after a separate provider normalizer is wired. No force option; crashes require manual reconciliation of the row.
    """
    if not _number(timeout_seconds) or not 0 < timeout_seconds <= 120:
        raise Refused("bounded collector deadline required")
    if not _number(fallback_seconds) or not 0 < fallback_seconds <= 86400:
        raise Refused("positive bounded fallback required")
    with ledger.tx() as con:
        binding = con.execute(select(aliases).where(aliases.c.id == alias)).mappings().first()
        if not binding:
            raise Refused("unknown account")
        account = binding["account"]
        ledger.lock(con, ["account:" + account])
        if not con.execute(select(accounts.c.id).where(accounts.c.id == account)).first():
            raise Refused("unknown account")
        now = time.time()
        until = con.execute(
            select(cooldowns.c.until).where(
                cooldowns.c.account == account, cooldowns.c.endpoint == "usage"
            )
        ).scalar_one_or_none()
        if until is not None and until > now:
            return {"status": "cooldown", "until": until, "observation": None}
        prior = (
            con.execute(select(collection_claims).where(collection_claims.c.account == account))
            .mappings()
            .first()
        )
        if prior and prior["token"]:
            return {
                "status": "busy" if prior["deadline"] > now else "reconciliation_required",
                "observation": None,
            }
        token = uuid.uuid4().hex
        deadline = now + timeout_seconds
        values = dict(token=token, deadline=deadline, last_status="collecting")
        if prior:
            con.execute(
                update(collection_claims)
                .where(collection_claims.c.account == account)
                .values(**values)
            )
        else:
            con.execute(collection_claims.insert().values(account=account, **values))
    # No database transaction is held during network work.
    result = {"status": "collector_error", "observation": None}
    persisting_cooldown = False
    try:
        response = collector(deadline=deadline)
        finished = time.time()
        if not isinstance(response, CollectionResponse):
            result["status"] = "invalid_response"
        else:
            action = classify_quota_status(response.status)
            received = finished if response.received_at is None else response.received_at
            if not _number(received) or not now <= received <= finished:
                if action == "cooldown":
                    return {"status": "reconciliation_required", "observation": None}
                result["status"] = "invalid_response_time"
            elif action == "cooldown":
                persisting_cooldown = True
                try:
                    until = retry_deadline(response.retry_after, received, fallback_seconds)
                except (ValueError, OverflowError):
                    return {"status": "reconciliation_required", "observation": None}
                persisting_cooldown = True
                stored = ledger.defer(account, "usage", until)
                persisting_cooldown = False
                result.update(status="cooldown", until=stored["until"])
            elif finished > deadline:
                result["status"] = "deadline_exceeded"
            elif action == "validate":
                # Normalization/admission belongs to a caller-supplied provider adapter.
                # Do not return raw observations that might include credentials.
                observed_status = (
                    response.observation.get("status")
                    if isinstance(response.observation, dict)
                    else None
                )
                result["status"] = {"unknown": "unknown", "error": "provider_error"}.get(
                    observed_status if isinstance(observed_status, str) else "",
                    "response_ok_unvalidated",
                )
            else:
                result["status"] = action
    except Exception:
        if persisting_cooldown:
            return {"status": "reconciliation_required", "observation": None}
        # Never export exception text, response bodies, headers or credentials.
        result = {"status": "collector_error", "observation": None}
    with ledger.tx() as con:
        ledger.lock(con, ["account:" + account])
        changed = con.execute(
            update(collection_claims)
            .where(collection_claims.c.account == account, collection_claims.c.token == token)
            .values(token=None, last_status=result["status"])
        )
        if changed.rowcount != 1:
            return {"status": "reconciliation_required", "observation": None}
    return result
