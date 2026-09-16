"""The plan node: a lane drafts a packet from a ticket, from the scout's facts.

Every brief so far was written by the coordinator by hand. A ticket names a title, a body
and a repository (optionally the paths it is about); the facts a brief opens with — which
files exist, which commits last touched them, which tests pin them, what the previous
lane reported — are all queries against git and the tree, and `lanes/scout.py::orient`
already renders them as a section. This module joins the two:

- `plan_task(board_dir, ticket, lane)` authors one board task of category `plan` from a
  ticket. The ticket's paths (or paths guessed from its body by plain grep of the
  identifiers) are scouted, the orientation rides in the task's brief, and the lane is
  asked for a single artifact, `packet.md`: a packet section in the brief format
  (`#### <id>. <title>`, location, acceptance tests, size, operator step) plus a fenced
  JSON block naming the packet's `gates` and `tests`.
- `settle_plan(...)` is the runner's code node over that artifact. It validates the block
  (through `validate_packet_task`, so the drafted packet is accepted by the same checks a
  hand-written one is), writes the packet's brief and a `packet` task file in state
  `ready`, records the id in the board's drafts list and returns what it created. A
  malformed heading or block raises, and the runner blocks the plan task with the reason.

The draft is not a build: a packet task the plan node authored waits in `drafts.json`
until the operator releases it or the board config carries `auto_dispatch: true`. Category
`plan` is declared by the lanes the operator marks `tier: plan` (J2); the runner refuses to
plan on any other tier.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from ..lanes.brief import PACKET_HEADING, hard_rules
from ..lanes.route import default_lanes
from ..lanes.scout import orient
from .packet_task import BASE_REF, validate_packet_task
from .task import validate_task

PLAN_CATEGORY = "plan"
PACKET_CATEGORY = "packet"
# The board-owned drafts list: packet tasks the plan node authored, held out of dispatch
# until the operator releases them (or the board config sets auto_dispatch). It lives
# beside the task files but is not one, so the board loader skips it by name.
DRAFTS_FILE = "drafts.json"
DEFAULT_BASE = "main"
DEFAULT_MAX_ROUNDS = 3
# The plan lane drafts; the build lane builds. A plan brief is small (a ticket plus the
# scout's section) but the drafting turn writes a whole packet section.
PLAN_BUDGET = {"wall_seconds": 1200, "output_bytes": 2000000, "thinking_tokens": 12000}
PACKET_BUDGET = {"wall_seconds": 3600, "output_bytes": 10000000, "thinking_tokens": 6000}

# The hard-rules section every packet brief must open with (`lanes/brief.py::hard_rules`
# cuts it by heading). This is the plan node's default — the drafting lane needs the same
# rules it is drafting under, and a packet brief is composed from what the planner was
# given. An operator with a project-specific umbrella brief edits the created brief.
HARD_RULES = """## 1. Hard rules

### Never read or edit
- Keep the ticket's repository read-only: scout it, never rewrite history.
- Credential stores, `.env*` files and personal data are never read or written.

### Never run
- No `git push`, no tags, no releases; the coordinator pushes after CI.
- Nothing that installs packages system-wide or edits launchd.

### Never write
- Credentials, tokens or absolute home paths in code, tests, docs or commit messages.

### Tests must stay offline
- No test opens a network connection; lane tests use fake CLIs and injected senders.
"""

PRODUCE = """## What to produce

Write exactly one file, `packet.md`, in the current directory. It is a packet in the brief
format a build lane reads:

- one section headed `#### <id>. <title>`, where `<id>` is an uppercase letter and up to
  three digits (`K1`); the section states the location (the files it will touch, as
  `` `path` `` tokens), the acceptance tests, the size and the one operator step;
- then one fenced `json` block holding exactly two keys:

```json
{"gates": [{"name": "tests", "argv": ["python", "-m", "pytest", "-q"]}],
 "tests": ["tests/test_example.py"]}
```

`gates` is the list of commands the harness runs after every build round (each needs a
`name` and an `argv`; `cwd`, `timeout` and `env` are optional). `tests` is the list of
repository-relative test files the packet must add or keep green. Nothing else: no prose
after the block.
"""


def slug(text: str, limit: int = 48) -> str:
    """A board-safe id fragment from a title: lowercase, hyphens, never empty."""
    cleaned = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    cleaned = cleaned[:limit].rstrip("-")
    return cleaned or "ticket"


# Tokens a ticket body can name a path in: a backticked run, or a bare token with a
# directory part or a source suffix.
BACKTICKED = re.compile(r"`([^`\n]+)`")
BARE_PATH = re.compile(r"\b((?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+)\b")
SUFFIXED = re.compile(r"\b([A-Za-z0-9_.-]+\.(?:py|md|json|txt|ts|tsx|js|toml|yaml|yml))\b")
IDENTIFIER = re.compile(r"\b[a-z][a-z0-9_]{3,}\b")


def _git(repo: Path, *args: str) -> str:
    import subprocess

    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    return proc.stdout if proc.returncode == 0 else ""


def _path_like(token: str) -> bool:
    if not token or " " in token or any(c in token for c in "<>|*?\"'"):
        return False
    return "/" in token or bool(re.search(r"\.(py|md|json|txt|ts|tsx|js|toml|yaml|yml)$", token))


def guess_paths(repo: Path, body: str, limit: int = 8) -> list[str]:
    """The paths a ticket body names, or the files its identifiers are grepped out of.

    A path-shaped token (backticked, slash-bearing or source-suffixed) is taken as one.
    Otherwise the body's snake_case identifiers are grepped through git, and the first
    `limit` tracked files that mention one are the guessed scope — plain grep, never a
    model, so the answer is deterministic for a given tree.
    """
    found: list[str] = []
    for token in BACKTICKED.findall(body or ""):
        token = token.strip()
        if _path_like(token) and token not in found:
            found.append(token)
    for pattern in (BARE_PATH, SUFFIXED):
        for token in pattern.findall(body or ""):
            if token in found or not _path_like(token):
                continue
            # `src/pkg/worker.py` also matches the bare-suffix pattern as `worker.py`;
            # keep the longest form of a path the body already named.
            if any(existing.endswith("/" + token) for existing in found):
                continue
            found.append(token)
    if found:
        return found[:limit]
    for word in list(dict.fromkeys(IDENTIFIER.findall(body or "")))[:12]:
        for line in _git(repo, "grep", "-l", "-w", word).splitlines():
            line = line.strip()
            if line and line not in found:
                found.append(line)
        if len(found) >= limit:
            break
    return found[:limit]


def plan_brief(title: str, body: str, repo: Path, paths: list[str], orientation: str) -> str:
    """The plan task's brief: the hard rules, the ticket, the scout's facts, the ask."""
    named = ", ".join(f"`{p}`" for p in paths) if paths else "(none named; read the body)"
    return (
        HARD_RULES
        + "\n---\n\n## Ticket\n\n"
        + f"- Title: {title}\n"
        + f"- Repository: {repo}\n"
        + f"- Paths: {named}\n\n"
        + (body.strip() + "\n\n" if body and body.strip() else "")
        + orientation
        + "\n"
        + PRODUCE
    )


def validate_plan_task(raw):
    """The plan shape: the standard task keys plus `spec {base, paths?}`; category `plan`.

    `board/task.py` is provider-authored (integrated unmodified), so the shared key checks
    are reused by validating a copy whose category is one it accepts; everything
    plan-specific is checked here. Defaults are applied at authoring, not here.
    """

    def err(k, m):
        raise ValueError(k + ": " + m)

    if not isinstance(raw, dict):
        err("task", "expected a dict")
    if raw.get("category") != PLAN_CATEGORY:
        err("category", "expected plan")
    if "spec" not in raw:
        err("task", "missing spec key")
    core = {k: v for k, v in raw.items() if k != "spec"}
    core["category"] = "pure_function"
    validate_task(core)
    spec = raw["spec"]
    if not isinstance(spec, dict):
        err("spec", "expected a dict")
    missing = {"base"} - set(spec)
    unknown = set(spec) - {"base", "paths"}
    if missing:
        err("spec", "missing keys: " + ", ".join(sorted(missing)))
    if unknown:
        err("spec", "unknown keys: " + ", ".join(sorted(unknown)))
    base = spec["base"]
    if not isinstance(base, str) or not BASE_REF.fullmatch(base) or ".." in base or len(base) > 80:
        err("spec", "base must be a branch or ref name in the project repository")
    paths = spec.get("paths", [])
    if not isinstance(paths, list) or not all(isinstance(p, str) and p for p in paths):
        err("spec", "paths must be a list of non-empty strs")
    out = {
        k: (dict(v) if k == "budget" else list(v) if isinstance(v, list) else v)
        for k, v in raw.items()
        if k != "spec"
    }
    out["spec"] = {"base": base, "paths": list(paths)}
    return out


def plan_task(board_dir, ticket, lane, task_id=None):
    """Author one plan task from a ticket; returns the created paths and the scouted paths.

    The ticket is `{title, body, repo, paths?}`: `repo` is the repository to scout (and
    the tree the packet's brief is written under), and `paths` names what the packet is
    about — absent, they are guessed from the body. The drafted packet's base is `main`
    (the ticket names none); the operator edits the draft when the project's base
    differs. Existing files are refused, like every authoring path.

    `task_id` overrides the id derived from the title. The runner's fix-packet drafting
    passes a deterministic id derived from (failed task, reason) so that the task file's
    own existence is the record that the plan was already drafted.
    """
    if not isinstance(ticket, dict):
        raise ValueError("ticket: expected a dict")
    unknown = set(ticket) - {"title", "body", "repo", "paths"}
    if unknown:
        raise ValueError("ticket: unknown keys: " + ", ".join(sorted(unknown)))
    title = ticket.get("title")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("ticket: title is required")
    repo = ticket.get("repo")
    if not isinstance(repo, str) or not repo:
        raise ValueError("ticket: repo is required")
    if not isinstance(lane, str) or not lane:
        raise ValueError("lane is required")
    body = ticket.get("body") or ""
    if not isinstance(body, str):
        raise ValueError("ticket: body must be a str")
    repo = Path(repo)
    paths = [p for p in (ticket.get("paths") or []) if isinstance(p, str) and p]
    if not paths:
        paths = guess_paths(repo, body)
    task_id = task_id or ("plan-" + slug(title))
    brief_rel = f"grid/briefs/{task_id}.txt"
    task = validate_plan_task(
        {
            "id": task_id,
            "category": PLAN_CATEGORY,
            "brief": brief_rel,
            "inputs": [brief_rel],
            "tests": [],
            "artifacts": ["packet.md"],
            "lanes": [lane],
            "author_family": None,
            "budget": dict(PLAN_BUDGET),
            "state": "ready",
            "blocked_reason": None,
            "spec": {"base": DEFAULT_BASE, "paths": paths},
        }
    )
    board_dir, brief = Path(board_dir), repo / brief_rel
    task_file = board_dir / (task_id + ".json")
    for label, path in (("task file", task_file), ("brief", brief)):
        if path.exists():
            raise FileExistsError(f"{label} already exists: {path}")
    board_dir.mkdir(parents=True, exist_ok=True)
    brief.parent.mkdir(parents=True, exist_ok=True)
    task_file.write_text(json.dumps(task, indent=1) + "\n")
    brief.write_text(plan_brief(title, body, repo, paths, orient(repo, paths)))
    return {"id": task_id, "task": str(task_file), "brief": str(brief), "paths": paths}


def parse_packet(text: str):
    """The (packet_id, body, block) a plan lane's packet.md declares, or ValueError.

    The body runs from the `#### <id>. <title>` heading to the end of the file; the block
    is the first fenced JSON object naming `gates`. `tests` is required with it, `artifacts`
    is optional, and any other key is refused — the block is a contract, not prose.
    """
    heading = PACKET_HEADING.search(text)
    if heading is None:
        raise ValueError("packet.md has no `#### <id>. <title>` heading")
    body = text[heading.start() :].strip()
    syntax = None
    for fence in re.finditer(r"```(?:json)?[ \t]*\n(.*?)```", body, re.S):
        try:
            data = json.loads(fence.group(1))
        except ValueError as exc:
            syntax = syntax or str(exc)
            continue
        if not isinstance(data, dict) or "gates" not in data:
            continue
        unknown = sorted(set(data) - {"gates", "tests", "artifacts"})
        if unknown:
            raise ValueError("packet.md block has unknown keys: " + ", ".join(unknown))
        if "tests" not in data:
            raise ValueError("packet.md block is missing the tests list")
        return heading.group(1), body, data
    if syntax is not None:
        raise ValueError("packet.md JSON block unreadable: " + syntax)
    raise ValueError("packet.md has no fenced JSON block naming the gates")


def packet_brief(rules: str, body: str) -> str:
    """The packet's brief document: the plan lane's hard rules, then the drafted section.

    `lanes/brief.py::packet_text` and `hard_rules` both read this document, so the plan
    lane never has to reproduce the rules verbatim — code carries them across.
    """
    return rules.rstrip() + "\n\n---\n\n" + body.strip() + "\n"


def settle_plan(task, board_dir, project_root, output_dir, lanes_view):
    """Turn the plan lane's packet.md into a drafted packet task; returns the created dict.

    Raises ValueError (a malformed heading or block, or the shared packet validation),
    FileExistsError (the packet task or brief already exists) or OSError (no packet.md).
    The runner blocks the plan task with the reason, so nothing partial is left behind.
    """
    text = (Path(output_dir) / "packet.md").read_text()
    packet_id, body, block = parse_packet(text)
    rules = hard_rules((Path(project_root) / task["brief"]).read_text())
    task_id = "packet-" + packet_id.lower()
    brief_rel = f"grid/briefs/{task_id}.txt"
    created = {
        "id": task_id,
        "category": PACKET_CATEGORY,
        "brief": brief_rel,
        "inputs": [brief_rel],
        "tests": list(block["tests"]),
        "artifacts": list(block.get("artifacts") or [f"docs/reports/{task_id}.md"]),
        "lanes": default_lanes(PACKET_CATEGORY, lanes_view),
        "author_family": None,
        "budget": dict(PACKET_BUDGET),
        "state": "ready",
        "blocked_reason": None,
        "spec": {
            "brief": brief_rel,
            "packet_id": packet_id,
            "gates": [dict(gate) for gate in block["gates"]],
            "base": task["spec"]["base"],
            "max_rounds": DEFAULT_MAX_ROUNDS,
        },
    }
    packet = validate_packet_task(created)
    board_dir, brief = Path(board_dir), Path(project_root) / brief_rel
    packet_file = board_dir / (task_id + ".json")
    for label, path in (("packet task", packet_file), ("packet brief", brief)):
        if path.exists():
            raise FileExistsError(f"{label} already exists: {path}")
    brief.parent.mkdir(parents=True, exist_ok=True)
    packet_file.write_text(json.dumps(packet, indent=1) + "\n")
    brief.write_text(packet_brief(rules, body))
    add_draft(board_dir, task_id)
    return {
        "id": task_id,
        "packet_id": packet_id,
        "task": str(packet_file),
        "brief": str(brief),
        "block": block,
    }


def drafts_path(board_dir) -> Path:
    return Path(board_dir) / DRAFTS_FILE


def load_drafts(board_dir) -> list[str]:
    """The packet task ids the plan node drafted and no operator has released yet."""
    try:
        data = json.loads(drafts_path(board_dir).read_text())
    except (OSError, ValueError):
        return []
    if isinstance(data, dict):
        data = data.get("drafts")
    if not isinstance(data, list):
        return []
    return [x for x in data if isinstance(x, str)]


def add_draft(board_dir, task_id) -> list[str]:
    """Record one drafted packet task; idempotent."""
    ids = load_drafts(board_dir)
    if task_id not in ids:
        ids.append(task_id)
    path = drafts_path(board_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"drafts": ids}, indent=1) + "\n")
    return ids
