"""`inference-grid state-migrate --repo <path> --dry-run`: grid runtime state out of a
product repo, into ~/.local/share/inference-grid/state/<repo>/, leaving a `grid.json`
(board name + gates) behind. Dry-run only tonight — `state_migrate()` refuses anything
but `dry_run=True`; the real move is a follow-up row, not written here.

Moves: `grid/board` (its `review/` subdirectory travels with it, nothing separate to
name), `grid/briefs`, `docs/handoff-glm-*.md` and `docs/reports/`. `grid.json`'s `gates`
({"tier", "allowed_prefixes"}) are read from the repo's own `grid/README.md` when one
exists (board_init's own template, board/new.py) — a repo with no board yet, or a
README that predates the template, leaves both None rather than guessing.
"""

import re
from pathlib import Path

MOVE_DIRS = ("grid/board", "grid/briefs")
DOC_DIRS = ("docs/reports",)
HANDOFF_GLOB = "docs/handoff-glm-*.md"

DEFAULT_STATE_ROOT = Path.home() / ".local/share/inference-grid/state"

TIER_RE = re.compile(r"Repository tier:\s*(\S+?)\.")
PREFIXES_RE = re.compile(r"Allowed input prefixes[^:]*:\s*(.+?)\.\s*\n")


def board_gates(repo):
    """(tier, allowed_prefixes) parsed from grid/README.md, or (None, None) absent one."""
    try:
        text = (Path(repo) / "grid" / "README.md").read_text()
    except OSError:
        return None, None
    tier = TIER_RE.search(text)
    prefixes = PREFIXES_RE.search(text)
    return (
        tier.group(1) if tier else None,
        [p.strip() for p in prefixes.group(1).split(",")] if prefixes else None,
    )


def migration_plan(repo, state_root=None):
    """Every path a real state-migrate would move, and the grid.json it would leave.

    Read-only: nothing here touches the repo or the state root. A move entry's `from`
    is only listed when that path actually exists in the repo today; an absent one is
    simply not planned, never invented.
    """
    repo = Path(repo).resolve()
    dest_root = (Path(state_root) if state_root else DEFAULT_STATE_ROOT) / repo.name
    moves = []
    for rel in MOVE_DIRS:
        src = repo / rel
        if src.exists():
            moves.append({"from": str(src), "to": str(dest_root / rel)})
    for src in sorted(repo.glob(HANDOFF_GLOB)):
        moves.append({"from": str(src), "to": str(dest_root / "docs" / src.name)})
    for rel in DOC_DIRS:
        src = repo / rel
        if src.exists():
            moves.append({"from": str(src), "to": str(dest_root / rel)})
    tier, prefixes = board_gates(repo)
    return {
        "repo": str(repo),
        "state_root": str(dest_root),
        "moves": moves,
        "grid_json": {
            "board": repo.name,
            "gates": {"tier": tier, "allowed_prefixes": prefixes},
        },
    }


def state_migrate(repo, dry_run=True, state_root=None):
    """The command's one entry point. `dry_run` must be True tonight (B6) — anything
    else refuses rather than silently no-op moving nothing, so a caller can't mistake
    a missing --dry-run flag for "moved for real"."""
    if dry_run is not True:
        raise ValueError("state-migrate only supports --dry-run tonight (B6); no mover exists yet")
    return migration_plan(repo, state_root=state_root)
