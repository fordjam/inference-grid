"""RabbitMQ transports IDs; the database authorizes execution."""

import os
import time

from celery import Celery
from sqlalchemy import select, update

from .ledger import Ledger, attempts, outbox
from .worker import execute

app = Celery(
    "inference_grid",
    broker=os.environ.get("GRID_BROKER_URL", "amqp://guest:guest@localhost:5672//"),
)
app.conf.update(
    task_default_queue=os.environ.get("GRID_QUEUE", "celery"),
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    event_queue_exclusive=True,
    control_queue_exclusive=True,
    worker_prefetch_multiplier=1,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    task_ignore_result=True,
    task_default_delivery_mode="persistent",
    broker_transport_options={"confirm_publish": True},
    task_publish_retry=False,
)


def database():
    return Ledger(os.environ["GRID_DATABASE_URL"])


@app.task(name="inference_grid.execute", autoretry_for=())
def run_attempt(aid, generation):
    return execute(database(), aid, generation)


def publish(ledger, sender=None):
    sender = sender or (lambda aid, generation: run_attempt.apply_async(args=[aid, generation]))
    with ledger.engine.connect() as con:
        rows = list(
            con.execute(
                select(outbox.c.attempt, attempts.c.generation)
                .join(attempts, attempts.c.id == outbox.c.attempt)
                .where(outbox.c.sent.is_(None))
            ).mappings()
        )
    for row in rows:
        sender(row["attempt"], row["generation"])
        # Crash between publish and this commit produces harmless duplicate delivery.
        with ledger.tx() as con:
            con.execute(
                update(outbox).where(outbox.c.attempt == row["attempt"]).values(sent=time.time())
            )
    return len(rows)


def hold_abandoned(ledger, before):
    """Operator-selected age triggers a hold, never permission to redispatch."""
    with ledger.engine.connect() as con:
        ids = list(
            con.execute(
                select(attempts.c.id).where(
                    attempts.c.state == "dispatching", attempts.c.updated < before
                )
            ).scalars()
        )
    for aid in ids:
        ledger.hold(aid, "worker recovery requires native reconciliation")
    return len(ids)
