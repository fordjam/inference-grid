# The autonomy policy

The grid runs unattended between operator visits: a board tick dispatches, requeues,
drafts, lands and publishes on its own. This document is the line between what the grid
does alone and what always waits for a person. Its executable form is
`src/inference_grid/board/policy.py` — the `ALONE` and `WAITS` constants and the
`owner_only` matcher. The table below is a tested mirror of those constants: a test
reads both, and a row that moves without its constant (or the reverse) fails the suite.

## The policy

| Decision | Action |
| --- | --- |
| alone | dispatch ready tasks within each account's admission limit |
| alone | requeue a dead attempt (L2) |
| alone | draft a fix packet for a blocked build (M1) |
| alone | land a packet whose gates and independent review passed, on a board with auto_land |
| alone | publish observations |
| waits | anything that changes published research numbers |
| waits | the holdout register |
| waits | credentials |
| waits | provider configuration |
| waits | deploys |
| waits | spend beyond the configured limit |
| waits | a packet whose files fall under a board's owner_only prefixes |

## Where each side lives

Alone is what the tick already does, and where:

- **Dispatch within the admission limit** — `board/runner.py::tick` routes every `ready`
  task through lane selection and admits only when the account has a free slot and the
  ledger's admission limits allow the spend. No attempt is claimed without a seat.
- **Requeue a dead attempt (L2)** — a task left `dispatched` by a crashed runtime, whose
  newest ledger attempt is terminal and unsuccessful, returns to `ready` with a ledger
  event (`board/runner.py::requeue_dead`). A live attempt and a terminal successful one
  are reported, never moved.
- **Draft a fix packet (M1)** — a packet the loop settled blocked gets a drafted fix
  packet (`board/fix_packet.py`), held in the board's drafts list. Drafting is free;
  building the draft still waits for the operator's release or an explicit board
  `auto_dispatch`.
- **Land a passed packet under `auto_land`** — on a board the operator configured with
  `auto_land`, the tick ends by landing every `passed` packet through `board/land.py`:
  the loop's gates re-run on the merged tree and the branch joins its base. A packet
  task reaches `passed` on its own in-lane gates alone — no per-commit review task is
  ever spawned for a packet; the branch-scope review, once a verify_merge task proves
  the merge, is what reads such a branch as a whole. Every other work task's `passed`
  is always gated by an approved independent review (B7: never a waiver).
- **Publish observations** — the collectors and `board_prepare` refresh quota readings
  and lane records so the next pass selects on current facts. Publication is the
  deliverable; it edits nothing in the project.

Waits is enforced mostly by absence — no code path writes research numbers, the holdout
register, credentials, provider configuration or a deploy — and in one place by a check:

- Published research numbers, the holdout register, credentials, provider configuration
  and deploys have no writer in the package. Landing a packet is the only way grid work
  reaches the tree, and `owner_only` (below) is how a board marks the paths where even
  that needs a person.
- **Spend beyond the configured limit** — the ledger refuses a claim that would exceed
  an account's admission limits; a task whose dispatch would overspend is refused, not
  dispatched anyway.
- **A packet under an `owner_only` prefix** — enforced at dispatch, next section.

## Owner-only prefixes

A board config may carry an `owner_only` key: a list of project-relative path prefixes
whose files no packet may touch without the operator. Default `[]` — no gate.

```json
{
  "board_dir": "grid/board",
  "project_root": ".",
  "owner_only": ["docs/research", "calibration/holdout"]
}
```

A packet task is *owner-only* when one of its declared files — inputs (the brief among
them), tests, artifacts — falls under a prefix. The match is directory-style: prefix
`docs/research` covers `docs/research/numbers.md` and `docs/research` itself, never
`docs/research-notes.md`. The declared files are the whole of what a packet can touch:
staging copies exactly them into the attempt.

When the runner meets an owner-only packet it refuses to dispatch it: no lane is
selected, no attempt is opened, nothing is spent, and the task stays `ready`. The tick
row names the prefix that matched — `owner_only: <prefix>` — and the morning digest
lists the packet under *Needs you* with that prefix, so the reason a packet never moves
is where the operator already looks. Removing the prefix from the config (a deliberate
operator edit) is what releases the packet; the next tick dispatches it like any other.

`docs/AUTONOMY.md`'s table and `board/policy.py`'s constants change together; the test
that reads both is what keeps this document the policy and not a memo about one.
