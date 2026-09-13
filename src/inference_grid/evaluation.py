"""The scorecard as a document: `inference-grid evaluation --json {out?}`.

Renders the ledger's evidence as markdown — scorecard rows per family, model and
category (attempts, completions, acceptances, acceptance rate, mean tokens in/out where
recorded, last attempt time) followed by the per-account readiness view. The document
goes to stdout, or to the `out` file; never into a docs/ directory — the coordinator
decides when a regenerated docs/EVALUATION.md is committed.
"""

import time
from pathlib import Path

from .ledger import ACTIVE
from .ledger import accounts, aliases as alias_records, attempts as attempt_records
from .ledger import cooldowns, events, observations, select, tasks


def _last_attempt_times(ledger):
    """The newest attempt update per scorecard key (family, model, outcome category)."""
    times = {}
    with ledger.engine.connect() as con:
        specs = {r["id"]: r["spec"] for r in con.execute(select(tasks)).mappings()}
        categories = {}
        for e in con.execute(
            select(events.c.attempt, events.c.detail).where(events.c.kind == "outcome_recorded")
        ).mappings():
            detail = e["detail"] if isinstance(e["detail"], dict) else {}
            categories[e["attempt"]] = detail.get("category", "unrecorded")
        for r in con.execute(
            select(attempt_records.c.task, attempt_records.c.id, attempt_records.c.updated)
        ).mappings():
            spec = specs.get(r["task"], {})
            key = (
                spec.get("family", "?"),
                spec.get("model", "?"),
                categories.get(r["id"], "unrecorded"),
            )
            if key not in times or r["updated"] > times[key]:
                times[key] = r["updated"]
    return times


def _account_view(ledger, now):
    """Per-alias readiness facts: active load, quota freshness, observation age, cooldowns."""
    with ledger.engine.connect() as con:
        alias_rows = list(con.execute(select(alias_records)).mappings())
        account_rows = {r["id"]: r for r in con.execute(select(accounts)).mappings()}
        active = {}
        for r in con.execute(
            select(attempt_records.c.account).where(attempt_records.c.state.in_(ACTIVE))
        ).mappings():
            active[r["account"]] = active.get(r["account"], 0) + 1
        observed = {
            r["account"]: r["observed_at"]
            for r in con.execute(select(observations)).mappings()
        }
        cooldowns_by_account = {}
        for r in con.execute(select(cooldowns).where(cooldowns.c.until > now)).mappings():
            cooldowns_by_account[r["account"]] = cooldowns_by_account.get(r["account"], 0) + 1
    rows = []
    for alias in sorted(alias_rows, key=lambda a: a["id"]):
        account = account_rows.get(alias["account"])
        rows.append(
            {
                "alias": alias["id"],
                "account": alias["account"],
                "active": active.get(alias["account"], 0),
                "capacity": account["capacity"] if account else None,
                "expires": account["expires"] if account else None,
                "observed_at": observed.get(alias["account"]),
                "cooldowns": cooldowns_by_account.get(alias["account"], 0),
                "models": (account["models"] if account else None) or [],
            }
        )
    return rows


def _stamp(instant):
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(instant)) if instant else "—"


def evaluation_document(ledger, now=None):
    """The markdown document; a pure read over the ledger."""
    now = time.time() if now is None else now
    lines = [
        "# Evaluation",
        "",
        f"Generated {_stamp(now)} UTC from the ledger.",
        "",
        "## Scorecard",
        "",
        "| family | model | category | attempts | completed | accepted | acceptance rate"
        " | held | resolved | mean tokens in | mean tokens out | last attempt |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    times = _last_attempt_times(ledger)
    for row in ledger.scorecard():
        usage = row["usage"] if isinstance(row["usage"], dict) else {}
        reported = row["usage_reported"] or 0
        tin_total = usage.get("input_tokens", usage.get("input"))
        tout_total = usage.get("output_tokens", usage.get("output"))
        tin = f"{tin_total / reported:.0f}" if tin_total is not None and reported else "—"
        tout = f"{tout_total / reported:.0f}" if tout_total is not None and reported else "—"
        rate = f"{100 * row['accepted'] / row['attempts']:.0f}%" if row["attempts"] else "—"
        last = times.get((row["family"], row["model"], row["category"]))
        lines.append(
            f"| {row['family']} | {row['model']} | {row['category']} | {row['attempts']}"
            f" | {row['completed']} | {row['accepted']} | {rate} | {row['held']}"
            f" | {row['resolved']} | {tin} | {tout} | {_stamp(last)} |"
        )
    lines += [
        "",
        "## Account readiness",
        "",
        "| alias | account | active/capacity | quota expires | observed | cooldowns | models |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in _account_view(ledger, now):
        fresh = "fresh" if row["expires"] and row["expires"] > now else "stale"
        seen = "never" if row["observed_at"] is None else _stamp(row["observed_at"])
        capacity = "—" if row["capacity"] is None else f"{row['active']}/{row['capacity']}"
        lines.append(
            f"| {row['alias']} | {row['account']} | {capacity} | {fresh}"
            f" ({_stamp(row['expires'])}) | {seen} | {row['cooldowns']}"
            f" | {', '.join(row['models'])} |"
        )
    return "\n".join(lines) + "\n"


def write_document(text, out):
    """Write the document to out; a docs/ path is refused — that commit is the coordinator's."""
    path = Path(out)
    if "docs" in path.resolve().parts:
        raise ValueError("evaluation never writes into docs/; the coordinator commits it")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path
