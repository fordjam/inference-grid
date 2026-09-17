"""`inference-grid state-migrate --repo <path> --dry-run [--board <name>]`: grid runtime
state out of a product repo, into ~/.local/share/inference-grid/state/<name>/, leaving a
`grid.json` (board name + gates) behind. Dry-run only tonight — `state_migrate()` refuses
anything but `dry_run=True`; the real move is a follow-up row, not written here.

Moves: `grid/board` (its `review/` subdirectory travels with it, nothing separate to
name), `grid/briefs`, `docs/handoff-glm-*.md` and `docs/reports/`. `grid.json`'s `gates`
({"tier", "allowed_prefixes"}) are read from the repo's own `grid/README.md` when one
exists (board_init's own template, board/new.py) — a repo with no board yet, or a
README that predates the template, leaves both None rather than guessing.

`--repo` must be a directory with its own `.git` — a typo'd path is refused, never
planned as an empty no-op. The destination and `grid.json`'s board name default to the
repo directory's own basename, but two repos can share a basename (a worktree and its
origin checkout, say); when the auto-derived `state/<name>/` already exists on disk,
the plan is refused rather than silently reusing it for a possibly different repo — pass
`--board` explicitly to name the destination once you've confirmed it's the right one.
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


def migration_plan(repo, state_root=None, board=None):
    """Every path a real state-migrate would move, and the grid.json it would leave.

    Read-only: nothing here touches the repo or the state root. A move entry's `from`
    is only listed when that path actually exists in the repo today; an absent one is
    simply not planned, never invented.
    """
    repo = Path(repo).resolve()
    if not (repo.is_dir() and (repo / ".git").exists()):
        raise ValueError("state-migrate --repo must be a git repository: " + str(repo))
    board_name = board or repo.name
    base = Path(state_root) if state_root else DEFAULT_STATE_ROOT
    dest_root = base / board_name
    if board is None and dest_root.exists():
        raise ValueError(
            f"{dest_root} already exists; pass --board to confirm or disambiguate "
            "which repo it belongs to before planning a migration into it"
        )
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
            "board": board_name,
            "gates": {"tier": tier, "allowed_prefixes": prefixes},
        },
    }


def state_migrate(repo, dry_run=True, state_root=None, board=None):
    """The command's one entry point. `dry_run` must be True tonight (B6) — anything
    else refuses rather than silently no-op moving nothing, so a caller can't mistake
    a missing --dry-run flag for "moved for real"."""
    if dry_run is not True:
        raise ValueError("state-migrate only supports --dry-run tonight (B6); no mover exists yet")
    return migration_plan(repo, state_root=state_root, board=board)
