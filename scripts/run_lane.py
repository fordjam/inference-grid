"""Run handoff-brief packets through the packet loop, one lane, one clone, sequentially.

The previous driver lived in a scratch directory and is gone; this one is versioned.
For each packet id: branch from the base, orient (scout), prompt the agent under the
write sandbox, run the gates as code, re-enter the session on failure (packet.build_loop),
and on green fast-forward or merge the base branch in the coordinator's repo and record
the work with `inference-grid external`. A failed packet leaves its branch and its
attempt directory for the operator and the lane moves on.

Usage (operator, from the coordinator's checkout):

  python scripts/run_lane.py --repo . --clone ~/.grid-workspaces/ig-lane-a \\
      --brief docs/handoff-glm-14.md --packets A1 A2 --base glm/work \\
      --model z-ai/glm-5.3-flash --python ~/.local/share/inference-grid/venv/bin/python \\
      --packets-root ~/.grid-workspaces/packets --database sqlite:///.../board.sqlite
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from inference_grid.lanes import sandbox  # noqa: E402
from inference_grid.lanes.brief import (  # noqa: E402
    PACKET_HEADING,
    TRAILER,  # noqa: F401 — re-exported for callers of the script's helpers
    commit_gate_script,  # noqa: F401
    compose_prompt,
    family_of,
    hard_rules,
    mentioned_paths,
    packet_text,
    trailer_for,
)
from inference_grid.lanes.gates import gates_for  # noqa: E402
from inference_grid.lanes.packet import (  # noqa: E402
    ClineAdapter,
    CommandCodeAdapter,
    build_loop,
    compact_transcripts,
    fix_prompt,
    run_gates,
)
from inference_grid.lanes.scout import orient  # noqa: E402


def git(repo, *args, check=True):
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {proc.stderr.strip()}")
    return proc.stdout.strip()


def slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40]


UNION_FILES = ("docs/CONTRIBUTIONS.md", "docs/LANES.md")


class landing_lock:
    """A directory lock (mkdir is atomic) held while a branch lands on the base."""

    def __init__(self, path: Path, wait_s: int = 900):
        self.path, self.wait_s = path, wait_s

    def __enter__(self):
        deadline = time.monotonic() + self.wait_s
        while True:
            try:
                self.path.mkdir(parents=True, exist_ok=False)
                return self
            except FileExistsError:
                if time.monotonic() > deadline:
                    raise RuntimeError(f"landing lock held too long: {self.path}")
                time.sleep(3)

    def __exit__(self, *exc):
        try:
            self.path.rmdir()
        except OSError:
            pass


def union_resolve(wt: Path, files) -> bool:
    """Resolve conflicts only in `files`, keeping both sides; False if any other file conflicts."""
    conflicted = git(wt, "diff", "--name-only", "--diff-filter=U").split()
    if not conflicted or any(f not in files for f in conflicted):
        return False
    for f in conflicted:
        path = wt / f
        text = re.sub(
            r"<<<<<<< [^\n]*\n(.*?)=======\n(.*?)>>>>>>> [^\n]*\n",
            lambda m: m.group(1) + m.group(2),
            path.read_text(),
            flags=re.S,
        )
        path.write_text(text)
        git(wt, "add", f)
    return True


def base_worktree(repo: Path, base: str):
    """The worktree that has `base` checked out, if any."""
    out = git(repo, "worktree", "list", "--porcelain")
    path = None
    for line in out.splitlines():
        if line.startswith("worktree "):
            path = Path(line.split(" ", 1)[1])
        elif line == f"branch refs/heads/{base}":
            return path
    return None


def record_external(
    python: str,
    repo: Path,
    database: str,
    *,
    task: str,
    model: str,
    account: str,
    argv: list[str],
    workspace: Path,
    verdict: dict,
    accepted: bool,
    note: str,
):
    spec = {
        "task": task,
        "project": "inference-grid",
        "spec": {
            "authorized": True,
            "account": account,
            "model": model,
            "family": family_of(model),
            "argv": argv[:3] + ["..."],
            "workspace": str(workspace),
        },
        "receipt": {
            "verified_in_lane": bool(verdict.get("verified_in_lane")),
            "elapsed_s": verdict.get("elapsed_s"),
            "returncode": (verdict.get("rounds") or [{}])[-1].get("agent_returncode"),
        },
        "category": "packet",
        "accepted": accepted,
        "repairs": max(0, len(verdict.get("rounds") or []) - 1),
        "note": note[:400],
    }
    path = workspace.parent / f"{task}.external.json"
    path.write_text(json.dumps(spec))
    subprocess.run(
        [
            str(repo / ".venv/bin/inference-grid")
            if (repo / ".venv/bin/inference-grid").exists()
            else "inference-grid",
            "--database",
            database,
            "external",
            "--json",
            str(path),
        ],
        capture_output=True,
        text=True,
    )


def run_packet(args, packet_id: str, brief: str, rules: str, stamp: str) -> dict:
    repo, clone = Path(args.repo).resolve(), Path(args.clone).resolve()
    title = PACKET_HEADING.search(packet_text(brief, packet_id)).group(2)
    branch = f"{args.branch_prefix}/{packet_id.lower()}-{slug(title)}"
    report_name = f"glm-brief-14-{packet_id.lower()}.md"
    git(clone, "fetch", "-q", "origin")
    resuming = args.resume and git(clone, "branch", "--list", branch)
    if resuming:
        # Keep the branch and whatever the previous session committed; gates decide next.
        git(clone, "checkout", "-q", branch)
    else:
        # Fresh branch from the base as it stands now in the coordinator's repo.
        git(clone, "checkout", "-q", "-B", branch, f"origin/{args.base}")
    packet = packet_text(brief, packet_id)
    orientation = orient(clone, mentioned_paths(packet))
    prompt = compose_prompt(
        rules,
        orientation,
        packet,
        branch=branch,
        base=f"origin/{args.base}",
        python=args.python,
        report_name=report_name,
        trailer=trailer_for(args.model),
    )
    attempt_dir = Path(args.packets_root).expanduser() / f"lane-{args.lane}-14-{packet_id}" / stamp
    attempt_dir.mkdir(parents=True, exist_ok=True)
    (attempt_dir / "prompt.txt").write_text(prompt)
    home = Path.home()
    profile = sandbox.write_profile(
        clone,
        attempt_dir / "lane.sb",
        extra_write_roots=[home / ".commandcode", attempt_dir],
        deny_read_roots=sandbox.deny_read_roots(),
    )
    sandbox.probe(profile, clone)
    if args.adapter == "cline":
        adapter = ClineAdapter(args.model, work=clone, data_dir=attempt_dir / "cline-state")
        # An isolated --data-dir has no login; the key reaches only this process's
        # environment, read here by the harness from a 0600 file (as lanes/cline.py does).
        key_file = Path(args.cline_key_file).expanduser() if args.cline_key_file else None
        if key_file is None or not key_file.is_file():
            raise SystemExit(
                "--cline-key-file (mode 0600, one line: the key) is required for --adapter cline"
            )
        if (key_file.stat().st_mode & 0o777) != 0o600:
            raise SystemExit(f"{key_file} must be mode 0600")
        cline_key = key_file.read_text().strip()
    else:
        (attempt_dir / "grid-effort.mjs").write_text(
            "export default function (cmd) { cmd.on('session_start', () => { cmd.setEffort('high'); }); }\n"
        )
        adapter = CommandCodeAdapter(
            args.model,
            mod_path=attempt_dir / "grid-effort.mjs",
            session_name=f"lane-{args.lane}-14-{packet_id}-{stamp}",
        )
    # Nothing from this shell's own model configuration reaches the lane or its gates.
    inherited = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("ANTHROPIC_", "CLAUDE_", "OPENAI_"))
    }
    env = dict(
        inherited,
        HOME=str(home),
        TMPDIR=str(attempt_dir),
        COMMANDCODE_SKIP_UPDATES="1",
        DO_NOT_TRACK="1",
        PYTHONPATH="src",
    )
    if args.adapter == "cline":
        env["CLINE_API_KEY"] = cline_key
    gates = gates_for(
        args.python,
        f"origin/{args.base}",
        trailer_for(args.model),
        str(Path(args.packets_root).expanduser()),
    )
    print(f"[{args.lane}] {packet_id} → {branch} (attempt {attempt_dir})", flush=True)
    if resuming:
        (attempt_dir / "gates-0").mkdir(exist_ok=True)
        pre = run_gates(clone, gates, env, attempt_dir / "gates-0")
        if all(r.ok for r in pre):
            verdict = {
                "adapter": adapter.name,
                "reason": "gates_passed",
                "rounds": [],
                "gates_passed": True,
                "verified_in_lane": True,
                "elapsed_s": 0,
            }
            (attempt_dir / "verdict.json").write_text(json.dumps(verdict, indent=2))
            print(f"[{args.lane}] {packet_id} resumed: gates already green", flush=True)
        else:
            prompt = (
                prompt
                + "\n\n## Resuming\n\nA previous session on this branch already committed. "
                + "Do not start over: amend that commit (it must stay the only commit ahead of the base) "
                + "so that these gates pass.\n\n"
                + fix_prompt(pre, 1, args.max_rounds)
            )
            verdict = None
    else:
        verdict = None
    if verdict is None:
        verdict = build_loop(
            adapter,
            lambda argv: sandbox.command(profile, argv),
            clone,
            env,
            prompt,
            gates,
            attempt_dir,
            wall_seconds=args.wall_seconds,
            max_rounds=args.max_rounds,
        )
    compact_transcripts(attempt_dir)
    passed = bool(verdict.get("gates_passed"))
    head = git(clone, "rev-parse", "--short", "HEAD")
    if passed:
        # Bring the branch into the coordinator's repo and advance the base — through the
        # worktree that has it checked out when there is one (branch -f refuses otherwise).
        git(repo, "fetch", "-q", str(clone), f"{branch}:{branch}")
        wt = base_worktree(repo, args.base)
        if wt is None:
            merge_wt = Path(args.packets_root).expanduser() / f"merge-{args.base.replace('/', '-')}"
            if not merge_wt.exists():
                git(repo, "worktree", "add", "-q", str(merge_wt), args.base)
            wt = merge_wt
        # Landing is serialized across lanes: one merge at a time into the base worktree.
        with landing_lock(
            Path(args.packets_root).expanduser() / f"landing-{args.base.replace('/', '-')}.lock"
        ):
            git(wt, "checkout", "-q", args.base)
            try:
                git(wt, "merge", "-q", "--ff-only", branch)
                how = "fast-forwarded"
            except RuntimeError:
                try:
                    git(wt, "merge", "--no-edit", branch)
                    how = "merged"
                except RuntimeError:
                    # Independent packets each add a CONTRIBUTIONS row (and sometimes a LANES
                    # paragraph) in the same place; keep both sides for those files only.
                    if union_resolve(wt, UNION_FILES):
                        git(wt, "-c", "core.editor=true", "commit", "-q", "--no-edit")
                        how = "merged (union on " + ", ".join(UNION_FILES) + ")"
                    else:
                        git(wt, "merge", "--abort", check=False)
                        raise
        note = f"{packet_id} {title}: gates green in {len(verdict['rounds'])} round(s); {how} onto {args.base}"
    else:
        note = f"{packet_id} {title}: {verdict.get('reason')}; branch {branch} at {head} left for the operator"
    print(f"[{args.lane}] {packet_id} {'PASSED' if passed else 'FAILED'} — {note}", flush=True)
    if args.database:
        record_external(
            args.python,
            repo,
            args.database,
            task=f"lane-{args.lane}-14-{packet_id}-{stamp}",
            model=args.model,
            account="cline" if args.adapter == "cline" else "goat",
            argv=adapter.first("…"),
            workspace=attempt_dir,
            verdict=verdict,
            accepted=passed,
            note=note,
        )
    return {
        "packet": packet_id,
        "branch": branch,
        "head": head,
        "passed": passed,
        "reason": verdict.get("reason"),
        "rounds": len(verdict.get("rounds") or []),
        "elapsed_s": verdict.get("elapsed_s"),
    }


def main(argv=None):
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--repo", required=True, help="coordinator's checkout (origin for the clone)")
    p.add_argument("--clone", required=True, help="the lane's own clone; created if absent")
    p.add_argument("--brief", required=True, help="brief path relative to the repo")
    p.add_argument("--rules", default="docs/handoff-glm.md")
    p.add_argument("--packets", nargs="+", required=True)
    p.add_argument("--base", default="glm/work")
    p.add_argument("--branch-prefix", default="glm")
    p.add_argument("--lane", default="glm")
    p.add_argument("--model", default="z-ai/glm-5.3-flash")
    p.add_argument("--adapter", choices=("command_code", "cline"), default="command_code")
    p.add_argument(
        "--resume", action="store_true", help="keep an existing packet branch; gates decide"
    )
    p.add_argument(
        "--cline-key-file", default=None, help="0600 file holding the Cline API key (adapter cline)"
    )
    p.add_argument("--python", required=True, help="interpreter with the package's dependencies")
    p.add_argument("--packets-root", default="~/.grid-workspaces/packets")
    p.add_argument(
        "--database", default=None, help="ledger URL; records each packet via `external`"
    )
    p.add_argument("--wall-seconds", type=int, default=5400)
    p.add_argument("--max-rounds", type=int, default=3)
    args = p.parse_args(argv)
    repo, clone = Path(args.repo).resolve(), Path(args.clone).expanduser().resolve()
    if not clone.exists():
        subprocess.run(["git", "clone", "-q", "--shared", str(repo), str(clone)], check=True)
    brief = (repo / args.brief).read_text()
    rules = hard_rules((repo / args.rules).read_text())
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    results = [run_packet(args, pid, brief, rules, stamp) for pid in args.packets]
    summary = Path(args.packets_root).expanduser() / f"lane-{args.lane}-14-{stamp}.summary.json"
    summary.write_text(json.dumps(results, indent=2))
    print(json.dumps(results, indent=2))
    return 0 if all(r["passed"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
