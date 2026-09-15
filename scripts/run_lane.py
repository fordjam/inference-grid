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
from inference_grid.lanes.packet import CommandCodeAdapter, Gate, build_loop  # noqa: E402
from inference_grid.lanes.scout import orient  # noqa: E402

PACKET_HEADING = re.compile(r"^#### ([A-Z]\d+)\. (.+)$", re.M)
PATH_IN_TEXT = re.compile(
    r"`((?:src|tests|docs|deployments|calibration|scripts|grid)/[A-Za-z0-9_./-]+)`"
)
TRAILER = "Co-Authored-By: GLM-5.3-Flash <noreply@z.ai>"


def git(repo, *args, check=True):
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {proc.stderr.strip()}")
    return proc.stdout.strip()


def packet_text(brief: str, packet_id: str) -> str:
    """The packet's section: its heading through the line before the next heading."""
    matches = list(PACKET_HEADING.finditer(brief))
    for i, m in enumerate(matches):
        if m.group(1) == packet_id:
            end = len(brief)
            for later in matches[i + 1 :]:
                end = later.start()
                break
            phase = brief.find("\n## ", m.end())
            if phase != -1 and phase < end:
                end = phase
            return brief[m.start() : end].rstrip() + "\n"
    raise KeyError(f"packet {packet_id} not in brief")


def hard_rules(rules_doc: str) -> str:
    """Section 1 of the umbrella brief, verbatim."""
    start = rules_doc.find("## 1. Hard rules")
    end = rules_doc.find("\n---", start)
    if start == -1 or end == -1:
        raise ValueError("umbrella brief has no section 1")
    return rules_doc[start:end].rstrip() + "\n"


def mentioned_paths(text: str) -> list[str]:
    seen = []
    for p in PATH_IN_TEXT.findall(text):
        if p not in seen:
            seen.append(p)
    return seen


def slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40]


def compose_prompt(
    rules: str,
    orientation: str,
    packet: str,
    *,
    branch: str,
    base: str,
    python: str,
    report_name: str,
) -> str:
    return (
        "You are a build lane for this repository. Read every section below before you touch "
        "a file.\n\n"
        f"{rules}\n"
        "## How to work in this checkout\n\n"
        f"- You are on branch `{branch}`, branched from `{base}`. Commit on this branch only.\n"
        f"- Run the suite with `PYTHONPATH=src {python} -m pytest -q -p no:cacheprovider` "
        "(PYTHONPATH matters: the interpreter's installed package is a different checkout). "
        "Capture the list of failing tests BEFORE you change anything; some sandbox-only "
        "failures (process-group kills) are pre-existing and must be byte-identical after.\n"
        f"- `PYTHONPATH=src {python} -m ruff format` and `-m ruff check` must be clean.\n"
        f"- Finish with exactly ONE commit on this branch whose message explains why and ends "
        f"with the trailer `{TRAILER}`, one row in docs/CONTRIBUTIONS.md under "
        "`## 2026-09-15 — brief 14`, and your report at "
        f"`docs/reports/{report_name}`. Leave the working tree clean. Do not push.\n\n"
        f"{orientation}\n\n## Your packet\n\n{packet}"
    )


def commit_gate_script(base: str) -> str:
    """A code gate: exactly one commit ahead of base, trailer present, tree clean."""
    return (
        "import subprocess,sys\n"
        f"base={base!r}\n"
        "def g(*a):return subprocess.run(['git',*a],capture_output=True,text=True).stdout\n"
        "n=len([l for l in g('log','--oneline',base+'..HEAD').splitlines() if l])\n"
        "msg=g('log','-1','--format=%B')\n"
        "dirty=g('status','--porcelain')\n"
        "problems=[]\n"
        "if n!=1:problems.append(f'expected exactly one commit ahead of {base}, found {n}')\n"
        f"if {TRAILER!r} not in msg:problems.append('commit trailer missing: {TRAILER}')\n"
        "if dirty.strip():problems.append('working tree not clean:\\n'+dirty)\n"
        "print('\\n'.join(problems) or 'commit gate ok')\n"
        "sys.exit(1 if problems else 0)\n"
    )


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


def gates_for(python: str, base: str, gate_dir: Path) -> list[Gate]:
    gate_dir.mkdir(parents=True, exist_ok=True)
    commit_check = gate_dir / "commit_gate.py"
    commit_check.write_text(commit_gate_script(base))
    env = {"PYTHONPATH": "src"}
    return [
        Gate(
            "pytest",
            [python, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-x"],
            env=env,
            timeout=2400,
        ),
        Gate("ruff-format", [python, "-m", "ruff", "format", "--check", "."], env=env, timeout=300),
        Gate("ruff-check", [python, "-m", "ruff", "check", "."], env=env, timeout=300),
        Gate(
            "no-home-paths",
            [
                python,
                "-c",
                "import subprocess,sys;out=subprocess.run(['git','grep','-n','/Users/','--','src','tests','docs/handoff-glm-14.md','deployments/local','scripts'],capture_output=True,text=True).stdout;print(out or 'no home paths');sys.exit(1 if out else 0)",
            ],
            timeout=60,
        ),
        Gate("commit", [python, str(commit_check)], timeout=60),
    ]


def record_external(
    python: str,
    repo: Path,
    database: str,
    *,
    task: str,
    model: str,
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
            "account": "goat",
            "model": model,
            "family": "glm",
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
    # Fresh branch from the base as it stands now in the coordinator's repo.
    git(clone, "fetch", "-q", "origin")
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
    (clone / "grid-effort.mjs").write_text(
        "export default function (cmd) { cmd.on('session_start', () => { cmd.setEffort('high'); }); }\n"
    )
    adapter = CommandCodeAdapter(
        args.model,
        mod_path=clone / "grid-effort.mjs",
        session_name=f"lane-{args.lane}-14-{packet_id}-{stamp}",
    )
    env = dict(
        os.environ,
        HOME=str(home),
        TMPDIR=str(attempt_dir),
        COMMANDCODE_SKIP_UPDATES="1",
        DO_NOT_TRACK="1",
        PYTHONPATH="src",
    )
    gates = gates_for(args.python, f"origin/{args.base}", attempt_dir / "gate-scripts")
    print(f"[{args.lane}] {packet_id} → {branch} (attempt {attempt_dir})", flush=True)
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
        git(wt, "checkout", "-q", args.base)
        try:
            git(wt, "merge", "-q", "--ff-only", branch)
            how = "fast-forwarded"
        except RuntimeError:
            git(wt, "merge", "--no-edit", branch)
            how = "merged"
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
