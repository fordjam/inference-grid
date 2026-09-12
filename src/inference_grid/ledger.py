"""Authoritative admission and attempt transitions; PostgreSQL production target.

SQLite is supported only for local evaluation, with serialized write transactions.
No expired lease or worker timeout releases an ambiguous provider reservation.
"""

import hashlib
import json
import time
import uuid
from contextlib import contextmanager

from sqlalchemy import (
    JSON,
    Column,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    select,
    update,
)

from .receipts import safe_path, sha256, validate_receipt

metadata = MetaData()
accounts = Table(
    "accounts",
    metadata,
    Column("id", String, primary_key=True),
    Column("capacity", Integer, nullable=False),
    Column("generation", Integer, nullable=False),
    Column("windows", JSON, nullable=False),
    Column("expires", Float, nullable=False),
    Column("models", JSON, nullable=False),
)
observations = Table(
    "observations",
    metadata,
    Column("account", String, primary_key=True),
    Column("observed_at", Float, nullable=False),
    Column("snapshot_digest", String, nullable=False),
)
aliases = Table(
    "aliases",
    metadata,
    Column("id", String, primary_key=True),
    Column("account", String, nullable=False),
)
tasks = Table(
    "tasks",
    metadata,
    Column("id", String, primary_key=True),
    Column("project", String, nullable=False),
    Column("spec", JSON, nullable=False),
)
attempts = Table(
    "attempts",
    metadata,
    Column("id", String, primary_key=True),
    Column("task", String, nullable=False),
    Column("account", String, nullable=False),
    Column("generation", Integer, nullable=False),
    Column("state", String, nullable=False),
    Column("estimate", JSON, nullable=False),
    Column("workspace", String, nullable=False),
    Column("receipt", JSON),
    Column("reason", String),
    Column("updated", Float, nullable=False),
)
outbox = Table(
    "outbox",
    metadata,
    Column("attempt", String, primary_key=True),
    Column("sent", Float),
)
events = Table(
    "events",
    metadata,
    Column("id", String, primary_key=True),
    Column("attempt", String),
    Column("kind", String, nullable=False),
    Column("detail", JSON, nullable=False),
    Column("at", Float, nullable=False),
)
# Locks cover task/account/workspace namespaces; rows persist to avoid ABA races.
locks = Table("locks", metadata, Column("id", String, primary_key=True))
ACTIVE = ("queued", "dispatching", "held")


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class Refused(ValueError):
    pass


class Ledger:
    def __init__(self, url):
        self.engine = create_engine(url)
        self.sqlite = self.engine.dialect.name == "sqlite"

    def initialize(self):
        metadata.create_all(self.engine)

    @contextmanager
    def tx(self):
        with self.engine.connect() as con:
            if self.sqlite:
                con.exec_driver_sql("BEGIN IMMEDIATE")
            else:
                con.begin()
            try:
                yield con
                con.commit()
            except BaseException:
                con.rollback()
                raise

    def lock(self, con, keys):
        if self.sqlite:
            return
        from sqlalchemy.dialects.postgresql import insert

        for key in sorted(set(keys)):
            con.execute(insert(locks).values(id=key).on_conflict_do_nothing())
            con.execute(select(locks).where(locks.c.id == key).with_for_update()).first()

    def event(self, con, attempt, kind, **detail):
        con.execute(
            events.insert().values(
                id=str(uuid.uuid4()),
                attempt=attempt,
                kind=kind,
                detail=detail,
                at=time.time(),
            )
        )

    def configure_account(
        self, name, capacity, windows, expires, models, alias_names=(), observed_at=None
    ):
        import math

        if type(capacity) is not int or capacity < 1 or not models or not windows:
            raise Refused("invalid account configuration")
        if not math.isfinite(expires) or expires <= time.time():
            raise Refused("expired observation")
        for remaining in windows.values():
            if type(remaining) not in (int, float) or not math.isfinite(remaining) or remaining < 0:
                raise Refused("invalid remaining quota")
        observed_at = time.time() if observed_at is None else observed_at
        if (
            type(observed_at) not in (int, float)
            or not math.isfinite(observed_at)
            or observed_at > time.time() + 1
            or observed_at >= expires
        ):
            raise Refused("invalid observation timestamp")
        snapshot_digest = digest(
            dict(capacity=capacity, windows=windows, expires=expires, models=models)
        )
        with self.tx() as con:
            self.lock(con, ["account:" + name] + ["alias:" + a for a in alias_names])
            previous = (
                con.execute(select(observations).where(observations.c.account == name))
                .mappings()
                .first()
            )
            if previous and observed_at < previous["observed_at"]:
                raise Refused("out-of-order quota observation")
            replay = previous and observed_at == previous["observed_at"]
            if replay and snapshot_digest != previous["snapshot_digest"]:
                raise Refused("observation identity reused with changed content")
            old = con.execute(select(accounts).where(accounts.c.id == name)).mappings().first()
            values = dict(capacity=capacity, windows=windows, expires=expires, models=models)
            if not replay:
                # A delayed observation predating local completion cannot erase that debit.
                completed = list(
                    con.execute(
                        select(attempts.c.estimate).where(
                            attempts.c.account == name,
                            attempts.c.state.in_(["completed", "accepted"]),
                            attempts.c.updated > observed_at,
                        )
                    ).scalars()
                )
                values["windows"] = {
                    k: max(0, v - sum(e.get(k, 0) for e in completed)) for k, v in windows.items()
                }
                if old:
                    con.execute(update(accounts).where(accounts.c.id == name).values(**values))
                else:
                    con.execute(accounts.insert().values(id=name, generation=0, **values))
                record = dict(observed_at=observed_at, snapshot_digest=snapshot_digest)
                if previous:
                    con.execute(
                        update(observations).where(observations.c.account == name).values(**record)
                    )
                else:
                    con.execute(observations.insert().values(account=name, **record))
                self.event(
                    con,
                    None,
                    "quota_observed",
                    account=name,
                    observed_at=observed_at,
                    snapshot_digest=snapshot_digest,
                )
            for alias in (name, *alias_names):
                existing = (
                    con.execute(select(aliases).where(aliases.c.id == alias)).mappings().first()
                )
                if existing and existing["account"] != name:
                    raise Refused("alias reassignment prohibited")
                if not existing:
                    con.execute(aliases.insert().values(id=alias, account=name))

    def submit(self, task, project, spec):
        from pathlib import Path

        if not task or not project or spec.get("authorized") is not True:
            raise Refused("explicit local authorization required")
        if not spec.get("model") or not spec.get("family"):
            raise Refused("model and family required")
        if (
            not isinstance(spec.get("argv"), list)
            or not spec["argv"]
            or not all(isinstance(x, str) for x in spec["argv"])
        ):
            raise Refused("argv required")
        if not Path(spec.get("workspace", "")).is_absolute():
            raise Refused("absolute isolated workspace required")
        if type(spec.get("timeout")) is not int or not 1 <= spec["timeout"] <= 3600:
            raise Refused("bounded timeout required")
        if type(spec.get("priority", 100)) is not int:
            raise Refused("integer priority required")
        candidates = spec.get("candidates", [])
        if not isinstance(candidates, list) or any(
            not isinstance(c, dict)
            or not isinstance(c.get("account"), str)
            or not c["account"]
            or not isinstance(c.get("estimate"), dict)
            for c in candidates
        ):
            raise Refused("invalid route candidates")
        from .aliases import canonical_account

        for candidate in candidates:
            canonical_account(candidate["account"], spec.get("account_aliases", {}))
        inputs = spec.get("inputs", {})
        if not isinstance(inputs, dict) or any(
            not safe_path(k) or not sha256(v) for k, v in inputs.items()
        ):
            raise Refused("invalid input manifest")
        if spec.get("manifest_sha256") != digest(inputs):
            raise Refused("manifest digest required")
        if inputs and not Path(spec.get("input_root", "")).is_absolute():
            raise Refused("absolute input root required")
        if type(spec.get("output_bytes")) is not int or not 1 <= spec["output_bytes"] <= 10_000_000:
            raise Refused("bounded output bytes required")
        spec = dict(spec, workspace=str(Path(spec["workspace"]).resolve()))
        with self.tx() as con:
            self.lock(con, ["task:" + task])
            old = con.execute(select(tasks).where(tasks.c.id == task)).mappings().first()
            if old:
                if old["spec"] != spec or old["project"] != project:
                    raise Refused("immutable task changed")
                return
            con.execute(tasks.insert().values(id=task, project=project, spec=spec))

    def claim(self, task, alias, estimate):
        import math

        if not estimate or any(
            type(v) not in (int, float) or not math.isfinite(v) or v <= 0 for v in estimate.values()
        ):
            raise Refused("positive quota estimates required")
        with self.tx() as con:
            binding = con.execute(select(aliases).where(aliases.c.id == alias)).mappings().first()
            job = con.execute(select(tasks).where(tasks.c.id == task)).mappings().first()
            if not binding or not job:
                raise Refused("unknown task or account")
            account = binding["account"]
            workspace = job["spec"]["workspace"]
            self.lock(con, ["account:" + account, "task:" + task, "workspace:" + workspace])
            acct = con.execute(select(accounts).where(accounts.c.id == account)).mappings().one()
            if acct["expires"] <= time.time() or job["spec"]["model"] not in acct["models"]:
                raise Refused("quota stale or model ineligible")
            rows = list(con.execute(select(attempts)).mappings())
            if any(r["task"] == task for r in rows):
                raise Refused("task already attempted; explicit new task required")
            active = [r for r in rows if r["state"] in ACTIVE]
            if any(r["workspace"] == workspace for r in active):
                raise Refused("workspace busy")
            shared = [r for r in active if r["account"] == account]
            if len(shared) >= acct["capacity"]:
                raise Refused("account busy")
            if set(estimate) != set(acct["windows"]):
                raise Refused("all quota windows must be reserved")
            for window, cost in estimate.items():
                reserved = sum(r["estimate"].get(window, 0) for r in shared)
                if reserved + cost > acct["windows"][window]:
                    raise Refused("insufficient quota")
            gen = acct["generation"] + 1
            aid = str(uuid.uuid4())
            con.execute(update(accounts).where(accounts.c.id == account).values(generation=gen))
            con.execute(
                attempts.insert().values(
                    id=aid,
                    task=task,
                    account=account,
                    generation=gen,
                    state="queued",
                    estimate=estimate,
                    workspace=workspace,
                    updated=time.time(),
                )
            )
            con.execute(outbox.insert().values(attempt=aid))
            self.event(con, aid, "admitted", generation=gen)
            return aid, gen

    def start(self, aid, generation):
        with self.tx() as con:
            prior = con.execute(select(attempts).where(attempts.c.id == aid)).mappings().first()
            if not prior or prior["generation"] != generation or prior["state"] != "queued":
                return None
            self.lock(con, ["account:" + prior["account"]])
            acct = (
                con.execute(select(accounts).where(accounts.c.id == prior["account"]))
                .mappings()
                .one()
            )
            job = con.execute(select(tasks).where(tasks.c.id == prior["task"])).mappings().one()
            reserved = list(
                con.execute(
                    select(attempts.c.estimate).where(
                        attempts.c.account == prior["account"],
                        attempts.c.state.in_(ACTIVE),
                    )
                ).scalars()
            )
            over_budget = any(
                sum(e.get(k, 0) for e in reserved) > v for k, v in acct["windows"].items()
            )
            changed_windows = any(set(e) != set(acct["windows"]) for e in reserved)
            if (
                acct["expires"] <= time.time()
                or job["spec"]["model"] not in acct["models"]
                or over_budget
                or changed_windows
            ):
                con.execute(
                    update(attempts)
                    .where(attempts.c.id == aid, attempts.c.state == "queued")
                    .values(
                        state="held",
                        reason="admission observation expired or model removed",
                    )
                )
                self.event(con, aid, "reconciliation_required", reason="stale admission")
                return None
            row = (
                con.execute(
                    update(attempts)
                    .where(
                        attempts.c.id == aid,
                        attempts.c.generation == generation,
                        attempts.c.state == "queued",
                    )
                    .values(state="dispatching", updated=time.time())
                    .returning(attempts)
                )
                .mappings()
                .first()
            )
            if not row:
                return None
            self.event(con, aid, "dispatch_intent")
            task = con.execute(select(tasks).where(tasks.c.id == row["task"])).mappings().one()
            return dict(row, spec=task["spec"])

    def hold(self, aid, reason):
        with self.tx() as con:
            changed = con.execute(
                update(attempts)
                .where(attempts.c.id == aid, attempts.c.state == "dispatching")
                .values(state="held", reason=reason, updated=time.time())
            )
            if changed.rowcount:
                self.event(con, aid, "reconciliation_required", reason=reason)

    def finish(self, aid, generation, receipt):
        with self.tx() as con:
            row = con.execute(select(attempts).where(attempts.c.id == aid)).mappings().one()
            self.lock(con, ["account:" + row["account"]])
            task = con.execute(select(tasks).where(tasks.c.id == row["task"])).mappings().one()
            errors = validate_receipt(
                receipt, task["spec"]["model"], task["spec"]["manifest_sha256"]
            )
            if errors:
                raise Refused("; ".join(errors))
            changed = con.execute(
                update(attempts)
                .where(
                    attempts.c.id == aid,
                    attempts.c.generation == generation,
                    attempts.c.state == "dispatching",
                )
                .values(state="completed", receipt=receipt, updated=time.time())
            )
            if changed.rowcount != 1:
                raise Refused("stale completion or ambiguous attempt")
            # Conservatively debit estimates until a new native observation replaces them.
            acct = (
                con.execute(select(accounts).where(accounts.c.id == row["account"]))
                .mappings()
                .one()
            )
            windows = {k: max(0, v - row["estimate"].get(k, 0)) for k, v in acct["windows"].items()}
            con.execute(
                update(accounts).where(accounts.c.id == row["account"]).values(windows=windows)
            )
            self.event(con, aid, "native_completed", receipt_digest=digest(receipt))

    def accept(self, aid, receipt_digest, reviewer_family, verdict):
        with self.tx() as con:
            self.lock(con, ["accept:" + aid])
            row = con.execute(select(attempts).where(attempts.c.id == aid)).mappings().one()
            task = con.execute(select(tasks).where(tasks.c.id == row["task"])).mappings().one()
            if row["state"] != "completed" or digest(row["receipt"]) != receipt_digest:
                raise Refused("candidate changed or not completed")
            if (
                not reviewer_family
                or reviewer_family == task["spec"]["family"]
                or verdict != "approved"
            ):
                raise Refused("independent approved review required")
            con.execute(update(attempts).where(attempts.c.id == aid).values(state="accepted"))
            self.event(
                con,
                aid,
                "operator_accepted",
                reviewer_family=reviewer_family,
                receipt_digest=receipt_digest,
            )

    def status(self):
        with self.engine.connect() as con:
            return [dict(r) for r in con.execute(select(attempts)).mappings()]
