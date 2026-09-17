"""The chore workflow: one deterministic script, five phases, typed envelopes.

02-C6's pilot for "one deterministic Python script per workflow type" (see
docs/WORKFLOWS-EVALUATION.md for the decision this follows: borrow the
phase/envelope/gate shape, adopt no external package). Code owns the
sequence and decides what each phase means; an agent is a bounded callable
that must hand back one typed `Envelope`, never more control than that.

Phases, in order:
  scout        (code)  list the files this chore names and the tests that
                        pin them -- `lanes.scout`, unchanged.
  build        (agent) the existing lane runner with the packet brief,
                        same-session repair on gate failure -- in
                        production this calls `lanes.packet.build_loop`;
                        here it is an injected callable so a fixture chore
                        can run end to end without a real agent CLI.
  gates        (code)  the task's own tests, as `lanes.packet.Gate`/
                        `run_gates` -- the same gate machinery the packet
                        lane already runs, called directly by this script
                        instead of hidden inside one generic runner.
  review       (agent) a different family than the builder; also an
                        injected callable, wired to a real cross-family
                        reviewer in a later row.
  land_request (code)  writes the land-request markdown the operator reads
                        and approves.

Every phase's envelope is logged to the ledger's `events` table (kind
`workflow_phase`) when a ledger is supplied -- no new table, the same
`Ledger.event` every other node in this repo already uses. Wiring the
build and review phases to real lanes is a later row; this one proves the
shape with fake agent callables end to end and a `--dry-run` phase-plan
printer.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from ..lanes import scout as scout_mod
from ..lanes.packet import Gate, run_gates

# name, kind ("code" or "agent"), one-line description -- the single source
# for both the dry-run phase plan and this module's own docstring above.
PHASES = (
    ("scout", "code", "list the files this chore names and the tests that pin them"),
    (
        "build",
        "agent",
        "the existing lane runner with the packet brief, same-session repair on gate failure",
    ),
    ("gates", "code", "the task's own tests"),
    ("review", "agent", "a different family than the builder"),
    ("land_request", "code", "writes the land-request markdown"),
)

REQUIRED_BRIEF_KEYS = ("id", "repo", "paths", "description")


@dataclass(frozen=True)
class Envelope:
    """One phase's typed, JSON-serializable output.

    `data` carries whatever the next phase needs (scout's file/test lists,
    build's branch name, review's verdict); `artifacts` names files the
    phase left on disk. `as_dict()` is exactly what gets logged to the
    ledger and what a real agent phase must parse its JSON into.
    """

    phase: str
    status: str  # "success" | "fail"
    summary: str
    data: Dict[str, Any] = field(default_factory=dict)
    artifacts: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "phase": self.phase,
            "status": self.status,
            "summary": self.summary,
            "data": self.data,
            "artifacts": list(self.artifacts),
        }


AgentCall = Callable[[Dict[str, Any], Dict[str, "Envelope"]], "Envelope"]


def load_brief(path) -> Dict[str, Any]:
    """A chore brief: `id`, `repo`, `paths`, `description`, optional `gates`
    (a list of `{name, argv, cwd?, timeout?, env?}`, the same shape a
    packet task's own gate list already uses)."""
    brief = json.loads(Path(path).read_text())
    missing = [key for key in REQUIRED_BRIEF_KEYS if key not in brief]
    if missing:
        raise ValueError("chore brief missing required key(s): " + ", ".join(missing))
    return brief


def phase_plan_lines(brief: Dict[str, Any]) -> List[str]:
    lines = [f"chore {brief['id']}: {brief['description']}"]
    for name, kind, description in PHASES:
        lines.append(f"  {name} ({kind}): {description}")
    return lines


def scout_phase(
    repo: Path, paths: Sequence[str], test_roots: Sequence[str] = ("tests",)
) -> Envelope:
    present = scout_mod.existing_paths(repo, paths)
    tests = scout_mod.referencing_tests(repo, paths, test_roots)
    found = sum(present.values())
    summary = (
        f"{found}/{len(present)} named file(s) exist; {len(tests)} test file(s) reference them"
    )
    return Envelope("scout", "success", summary, data={"files": present, "tests": tests})


def build_phase(
    brief: Dict[str, Any], envelopes: Dict[str, Envelope], build_agent: AgentCall
) -> Envelope:
    envelope = build_agent(brief, envelopes)
    if envelope.phase != "build":
        raise ValueError(f"build_agent must return a build envelope, got {envelope.phase!r}")
    return envelope


def gates_phase(repo: Path, gate_specs: Sequence[Dict[str, Any]], log_dir: Path) -> Envelope:
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    if not gate_specs:
        return Envelope("gates", "success", "no gates declared", data={"results": []})
    gates = [
        Gate(
            g["name"],
            list(g["argv"]),
            cwd=g.get("cwd", "."),
            timeout=g.get("timeout", 1800),
            env=dict(g.get("env") or {}),
        )
        for g in gate_specs
    ]
    results = run_gates(repo, gates, dict(os.environ), log_dir)
    ok = all(r.ok for r in results)
    passed = sum(r.ok for r in results)
    return Envelope(
        "gates",
        "success" if ok else "fail",
        f"{passed}/{len(results)} gate(s) passed",
        data={"results": [r.as_dict() for r in results]},
    )


def review_phase(
    brief: Dict[str, Any], envelopes: Dict[str, Envelope], review_agent: AgentCall
) -> Envelope:
    envelope = review_agent(brief, envelopes)
    if envelope.phase != "review":
        raise ValueError(f"review_agent must return a review envelope, got {envelope.phase!r}")
    return envelope


def _gates_report_text(gates_envelope: Envelope) -> str:
    results = gates_envelope.data.get("results", [])
    if not results:
        return "(no gates declared)"
    lines = []
    for result in results:
        lines.append(
            f"{result['name']}: {'ok' if result['ok'] else 'FAILED'} (exit {result['returncode']})"
        )
        if not result["ok"]:
            lines.append(str(result.get("tail", "")).strip())
    return "\n".join(lines)


def land_request_phase(
    brief: Dict[str, Any], envelopes: Dict[str, Envelope], out_path: Path
) -> Envelope:
    """Code writes the land request; nothing here decides whether it lands.

    `ready_to_land` mirrors `board/land.py`'s own `would_land`: true only
    when the gates phase passed and the reviewer's own verdict was
    "approved" -- an agent's summary text is never read as approval.
    """
    build, gates, review = envelopes["build"], envelopes["gates"], envelopes["review"]
    ready = gates.status == "success" and review.data.get("verdict") == "approved"
    if gates.status != "success":
        blocked_reason = "gates failed"
    elif review.data.get("verdict") != "approved":
        blocked_reason = "review did not approve"
    else:
        blocked_reason = None
    lines = [
        f"# Land request — {brief['id']}",
        "",
        f"### {brief['id']}",
        f"- What was asked: {brief['description']}",
        f"- Root cause / what changed: {build.summary}",
        f"- Branch: {build.data.get('branch', 'unknown')}",
        "- Tests run and output tail:",
        "```",
        _gates_report_text(gates),
        "```",
        f"- Reviewer family and findings: {review.data.get('family', 'unknown')} — {review.summary}",
        f"- Expected outcome: {brief['description']}",
        f"- Status: {'done' if ready else 'blocked (' + blocked_reason + ')'}",
        "- Approve?",
        "",
    ]
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines))
    return Envelope(
        "land_request",
        "success",
        f"land request written to {out_path}",
        data={"path": str(out_path), "ready_to_land": ready},
        artifacts=[str(out_path)],
    )


def _log_phase(ledger, workflow_id: str, envelope: Envelope) -> None:
    if ledger is None:
        return
    with ledger.tx() as con:
        ledger.event(
            con,
            None,
            "workflow_phase",
            workflow="chore",
            workflow_id=workflow_id,
            envelope=envelope.as_dict(),
        )


def run_chore(
    brief: Dict[str, Any],
    *,
    repo,
    build_agent: AgentCall,
    review_agent: AgentCall,
    report_dir,
    ledger=None,
    log_dir=None,
    test_roots: Sequence[str] = ("tests",),
    workflow_id: Optional[str] = None,
) -> Dict[str, Envelope]:
    """Run every phase in order; returns the phase name -> Envelope map.

    Each phase's envelope is logged to the ledger before the next phase
    starts, so a workflow interrupted mid-run leaves a true partial record
    rather than silence.
    """
    repo = Path(repo)
    report_dir = Path(report_dir)
    log_dir = Path(log_dir) if log_dir is not None else report_dir / "gate-logs"
    workflow_id = workflow_id or brief["id"]
    envelopes: Dict[str, Envelope] = {}

    def run_phase(name: str, make) -> Envelope:
        envelope = make()
        envelopes[name] = envelope
        _log_phase(ledger, workflow_id, envelope)
        return envelope

    run_phase("scout", lambda: scout_phase(repo, brief["paths"], test_roots))
    run_phase("build", lambda: build_phase(brief, envelopes, build_agent))
    run_phase("gates", lambda: gates_phase(repo, brief.get("gates", []), log_dir))
    run_phase("review", lambda: review_phase(brief, envelopes, review_agent))
    run_phase(
        "land_request",
        lambda: land_request_phase(brief, envelopes, report_dir / f"{workflow_id}.md"),
    )
    return envelopes


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(
        prog="python -m inference_grid.workflows.chore",
        description="Run (or plan) the chore workflow: scout -> build -> gates -> review -> land request.",
    )
    parser.add_argument(
        "brief", help="path to the chore brief JSON (id, repo, paths, description, optional gates)"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="print the phase plan and exit; runs no phase"
    )
    args = parser.parse_args(argv)
    brief = load_brief(args.brief)
    if args.dry_run:
        for line in phase_plan_lines(brief):
            print(line)
        return 0
    print(
        "wiring the build and review phases to real lanes is a later row; "
        "only --dry-run runs from the CLI today",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
