"""Deterministic task-level routing; no model classifier or silent fallback."""

from sqlalchemy import select

from .aliases import canonical_account
from .ledger import Refused, attempts, tasks


def tick(ledger):
    with ledger.engine.connect() as con:
        pending = list(
            con.execute(select(tasks).where(~tasks.c.id.in_(select(attempts.c.task)))).mappings()
        )
    pending.sort(key=lambda row: (row["spec"].get("priority", 100), row["id"]))
    outcomes = []
    for task in pending:
        # Candidate order is operator policy, not a price inferred from disparate units.
        candidates = task["spec"].get("candidates", [])
        reasons = []
        for candidate in candidates:
            try:
                alias = canonical_account(
                    candidate["account"], task["spec"].get("account_aliases", {})
                )
                aid, generation = ledger.claim(task["id"], alias, candidate["estimate"])
                outcomes.append(
                    dict(
                        task=task["id"],
                        state="queued",
                        attempt=aid,
                        generation=generation,
                    )
                )
                break
            except Refused as exc:
                reasons.append(str(exc))
        else:
            outcomes.append(
                dict(
                    task=task["id"],
                    state="blocked",
                    reasons=reasons or ["no authorized candidates"],
                )
            )
    return outcomes
