"""Brief documents as prompt material: packet sections, hard rules, prompts, the commit gate.

Moved verbatim out of scripts/run_lane.py so the board's packet tasks (board/packet_task.py)
and the operator's lane driver compose the same prompt from the same brief: a packet's
section is cut from its brief document by heading, the umbrella brief's section 1 rides
along as the rules, and the commit gate is generated code that pins the packet contract
(exactly one trailered commit on a clean tree, ahead of the base).
"""

from __future__ import annotations

import re

PACKET_HEADING = re.compile(r"^#### ([A-Z]\d+)\. (.+)$", re.M)
PATH_IN_TEXT = re.compile(
    r"`((?:src|tests|docs|deployments|calibration|scripts|grid)/[A-Za-z0-9_./-]+)`"
)
# The commit trailer names the model that produced the work; the family drives review
# exclusion downstream, so both come from the model id.
TRAILERS = {
    "glm": "Co-Authored-By: GLM-5.3-Flash <noreply@z.ai>",
    "deepseek": "Co-Authored-By: DeepSeek-V4.1-Flash <noreply@deepseek.com>",
    "kimi": "Co-Authored-By: Kimi-K3 <noreply@moonshot.ai>",
    "qwen": "Co-Authored-By: Qwen3.8 <noreply@alibabacloud.com>",
}
TRAILER = TRAILERS["glm"]


def family_of(model: str) -> str:
    m = model.lower()
    for fam in ("deepseek", "kimi", "qwen", "glm"):
        if fam in m:
            return fam
    return "unknown"


def trailer_for(model: str) -> str:
    return TRAILERS.get(family_of(model), TRAILER)


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


def compose_prompt(
    rules: str,
    orientation: str,
    packet: str,
    *,
    branch: str,
    base: str,
    python: str,
    report_name: str,
    trailer: str = TRAILER,
) -> str:
    return (
        "You are a build lane for this repository. Read every section below before you touch "
        "a file.\n\n"
        f"{rules}\n"
        "## How to work in this checkout\n\n"
        f"- You are on branch `{branch}`, branched from `{base}`. Commit on this branch only.\n"
        f"- Run the suite with `PYTHONPATH=src {python} -m pytest -q -p no:cacheprovider` "
        "(PYTHONPATH matters: the interpreter's installed package is a different checkout). "
        "The harness captures the base's failures and judges the branch on the ones it "
        "introduced; pre-existing failures are named `inherited` in the pytest gate.\n"
        f"- `PYTHONPATH=src {python} -m ruff format` and `-m ruff check` must be clean.\n"
        f"- Finish with exactly ONE commit on this branch whose message explains why and ends "
        f"with the trailer `{trailer}`, one row in docs/CONTRIBUTIONS.md under "
        "`## 2026-09-15 — brief 14`, and your report at "
        f"`docs/reports/{report_name}`. Leave the working tree clean. Do not push.\n"
        "- Keep working until that commit exists: a reply that uses no tool ends your "
        "session, and a session that ends without the commit is a failed round.\n\n"
        f"{orientation}\n\n## Your packet\n\n{packet}"
    )


def commit_gate_script(base: str, trailer: str = TRAILER) -> str:
    """The commit gate as a self-contained script: exactly one commit ahead of base, the
    trailer present, a clean tree. It imports nothing from this package so it runs in any
    interpreter the attempt's environment names (lanes/gates.py::run_commit is the same check
    for the driver's in-process gates)."""
    return (
        "import subprocess,sys\n"
        f"base={base!r}\n"
        "def g(*a):return subprocess.run(['git',*a],capture_output=True,text=True).stdout\n"
        "n=len([l for l in g('log','--oneline',base+'..HEAD').splitlines() if l])\n"
        "msg=g('log','-1','--format=%B')\n"
        "dirty=g('status','--porcelain')\n"
        "problems=[]\n"
        "if n!=1:problems.append(f'expected exactly one commit ahead of {base}, found {n}')\n"
        f"if {trailer!r} not in msg:problems.append('commit trailer missing: {trailer}')\n"
        "if dirty.strip():problems.append('working tree not clean:\\n'+dirty)\n"
        "print('\\n'.join(problems) or 'commit gate ok')\n"
        "sys.exit(1 if problems else 0)\n"
    )
