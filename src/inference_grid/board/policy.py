"""Review policy: waive the per-commit review the gates already proved.

The board creates one `independent_review` task per completed work task. That is the
right default: a second family reading the artifact catches what the author's own tests
cannot. It is the wrong answer when the source attempt's receipt already carries the
proof — a task whose declared gates all ran green inside the lane, and whose author is
not a first-party family, has been verified by code, and spending a reviewer on it buys
nothing. This module is that rule in one place, so the runner, the tests and the board
doc all read the same sentence.

`review_needed` is the decision; `record_waiver` writes it down. A waiver is never
silent: it lands as the source task's review record beside the board
(`review/<task-id>/review.json`) and, given a ledger, as a `review_waived` event. The
task file itself cannot carry it — `board/task.py` is provider-authored, integrated
unmodified, and refuses any key outside its fixed schema — so the record lives in the
source task's own review staging directory, where a review task's source link already
lives. Everything the gates did not prove keeps the current path: a review task.

Calibration is the evidence that earns the waiver. `board/calibration.py` measures a
reviewer's recall and false positives against answer keys; the lanes whose recorded
calibration recall a coordinator has read are the lanes whose green gates are trusted
here. The rule itself is mechanical — the recall is what makes it safe to turn on.
"""

import json
from pathlib import Path

# The sidecar name under <board>/review/<task-id>/; a review task's link is source.json.
REVIEW_RECORD = "review.json"


def review_needed(task, receipt, author_family):
    """(needed, reason): whether this passing task still needs a per-commit review.

    A review is needed unless the receipt proves the source lane's own gates ran and
    passed and the author family is one the grid does not review on principle:

    * `author_family == "claude"` — a first-party author is reviewed on every commit;
    * the receipt is missing or `verified_in_lane` is not `True` — the lane could not
      vouch for its own work;
    * the receipt declares no gate results, or any declared gate did not pass.

    The reason is written down either way: the waiver is recorded, and the current path
    (author a review task) is what a truthy answer means.
    """
    if author_family == "claude":
        return True, "author family claude is reviewed on every commit"
    if not isinstance(receipt, dict):
        return True, "no receipt: the attempt carries no evidence of its own gates"
    if receipt.get("verified_in_lane") is not True:
        return True, "the lane did not verify its own work (verified_in_lane is not true)"
    gates = receipt.get("gates")
    if not isinstance(gates, list) or not gates:
        return True, "the receipt declares no gate results"
    failed = [g for g in gates if not isinstance(g, dict) or g.get("ok") is not True]
    if failed:
        names = [str(g.get("name")) if isinstance(g, dict) else "?" for g in failed]
        return True, "declared gate(s) did not pass: " + ", ".join(names)
    return False, "waived: verified_in_lane and every declared gate passed"


def waiver_record(task, reason, attempt=None):
    """The review record a waived source task carries: `{"review": {"waived": true, …}}`."""
    return {
        "review": {
            "waived": True,
            "reason": reason,
            "task": task["id"],
            "attempt": attempt,
        }
    }


def record_waiver(board_dir, task, reason, ledger=None, attempt=None):
    """Write the waiver as the source task's review record and, given a ledger, an event.

    Returns the path written. The event is what makes the waiver visible in the ledger's
    own history; the sidecar is what makes it visible beside the board.
    """
    stage = Path(board_dir) / "review" / task["id"]
    stage.mkdir(parents=True, exist_ok=True)
    path = stage / REVIEW_RECORD
    path.write_text(json.dumps(waiver_record(task, reason, attempt), indent=1) + "\n")
    if ledger is not None and attempt is not None:
        with ledger.tx() as con:
            ledger.event(con, attempt, "review_waived", task=task["id"], reason=reason)
    return path
