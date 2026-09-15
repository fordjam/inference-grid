"""Mutation harness: classify a mutant by what the target does under it.

A mutant is `killed` when the target's verdict changes, `survived` when it does not, and
`invalid` when the mutant is not a runnable row at all. The classifier must never call an
invalid mutant a kill: that inflates the score with mutants the target never ran.
"""

import copy

MUTATIONS = {
    "drop_value": lambda row: {k: v for k, v in row.items() if k != "value"},
    "flip_value": lambda row: dict(row, value=not row["value"]),
}


def verdict(row):
    return {"id": row["id"], "value": row["value"]}


def run_mutant(name, row):
    return verdict(MUTATIONS[name](copy.deepcopy(row)))


def classify(name, row):
    """A mutant that cannot build its row is invalid; the un-tampered row always builds.

    The `except KeyError` below reads as if it could swallow a KeyError from the target
    and score a real kill as invalid. It cannot: `verdict` copies two keys the row already
    carries, so the only KeyError a run can raise comes from `MUTATIONS` removing the
    "value" key — and `classify` runs the un-tampered row, which still returns it.
    """
    try:
        mutated = run_mutant(name, row)
    except KeyError:
        return "invalid"
    return "killed" if mutated != verdict(row) else "survived"
