"""Packet board tasks: the build→gate→re-enter loop the board can dispatch itself.

Until now the packet loop ran only through the operator's scripts/run_lane.py, bypassing
board, admission and lane selection, and told the ledger afterwards via `external`. A
board task with category "packet" closes that: its spec names the brief document and the
packet id inside it, code gates, a round bound and a base branch. Dispatch admits the
attempt FIRST (submit, claim, workspace lease — the ledger attempt is open before any
model runs), then builds the loop's worktree as a scratch shared clone of the project
(never the operator checkout), branches it from the base and runs
lanes/packet.py::build_loop with the lane's adapter. On green gates the branch is fetched
into the project under packet/<task-id> and the attempt completes with the verdict as its
receipt; otherwise the attempt is held with the loop's reason and the branch is left for
the operator. The base branch is never advanced here — that stays an operator step.
"""

import hashlib
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

from ..ledger import Refused, digest
from ..lanes.brief import (
    commit_gate_script,
    trailer_for,
    compose_prompt,
    hard_rules,
    mentioned_paths,
    packet_text,
)
from ..lanes.packet import (
    DEFAULT_MIN_FREE_BYTES,
    ClineAdapter,
    CommandCodeAdapter,
    Gate,
    ZcodeAdapter,
    build_loop,
    free_disk_bytes,
)
from ..lanes.scout import orient
from .task import validate_task

# Lane kinds a packet task can run. Command Code and ZCode re-enter the same session on a
# fix round; the ClinePass CLI cannot (`--id` refuses a prompt in JSON mode), so its rounds
# are fresh sessions on the fix prompt — the branch and the gate output carry the context,
# exactly as the operator's driver does it. Kinds outside this set are refused with a reason.
PACKET_KINDS = ("goat_cli", "zcode_cli", "cline_cli")
UNSUPPORTED_ADAPTERS = {}

PACKET_ID = re.compile(r"[A-Z]\d{1,3}")
BASE_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/@+-]*")
SHA = re.compile(r"[0-9a-f]{40}")
EFFORT_MODULE_NAME = "grid-effort.mjs"
# Packets are whole-feature builds, not one-shot lane tasks: the effort module forces
# high, unlike the one-shot goat lane's low.
EFFORT_MODULE_TEXT = (
    "export default function (cmd) { cmd.on('session_start', () => { cmd.setEffort('high'); }); }"
)


def validate_board_task(raw):
    """The load/save entry the runner uses: packet, verify_merge, calibration_run and plan
    tasks validate here, the rest unchanged."""
    if isinstance(raw, dict) and raw.get("category") == "packet":
        return validate_packet_task(raw)
    if isinstance(raw, dict) and raw.get("category") == "plan":
        from .plan_task import validate_plan_task

        return validate_plan_task(raw)
    if isinstance(raw, dict) and raw.get("category") == "verify_merge":
        from .verify_merge import validate_verify_merge_task

        return validate_verify_merge_task(raw)
    if isinstance(raw, dict) and raw.get("category") == "calibration_run":
        from .calibration import validate_calibration_run_task

        return validate_calibration_run_task(raw)
    return validate_task(raw)


def validate_packet_task(raw):
    """The packet shape: the standard task keys plus `spec`; the category is "packet".

    board/task.py is provider-authored (integrated unmodified), so the shared key
    checks are reused by validating a copy whose category is one it accepts; everything
    packet-specific is checked here. Defaults are applied at dispatch, not here.

    A packet that landed carries one extra key, `landed`, and settles `landed` — a
    terminal state after `passed` that the provider-authored state list refuses, so
    board/land.py's transition is validated here: the record ({base_head, merge_commit,
    how}) and the state come together or not at all.

    Failover (brief J4) rides on two more packet-only keys, handled the same way:
    `failover` (bool, default true — the operator turns the one different-family retry
    off per task) and `failover_from` (the predecessor id, present only on a task the
    runner authored as a failover).
    """

    def err(k, m):
        raise ValueError(k + ": " + m)

    if not isinstance(raw, dict):
        err("task", "expected a dict")
    if raw.get("category") != "packet":
        err("category", "expected packet")
    if "spec" not in raw:
        err("task", "missing spec key")
    landed = raw.get("landed")
    failover = raw.get("failover", True)
    failover_from = raw.get("failover_from")
    if not isinstance(failover, bool):
        err("failover", "must be a bool")
    if failover_from is not None and (
        not isinstance(failover_from, str) or not failover_from or len(failover_from) > 60
    ):
        err("failover_from", "must be a non-empty str of at most 60 chars")
    core = {
        k: v for k, v in raw.items() if k not in ("spec", "landed", "failover", "failover_from")
    }
    core["category"] = "pure_function"
    if core.get("state") == "landed":
        # The shared check's nearest terminal state; the real state is restored below.
        core["state"] = "accepted"
    validate_task(core)
    if ("landed" in raw) != (raw.get("state") == "landed"):
        err("landed", "the landed record and the landed state come together or not at all")
    if landed is not None:
        if not isinstance(landed, dict) or set(landed) != {"base_head", "merge_commit", "how"}:
            err("landed", "must be a dict with exactly base_head, merge_commit, how")
        for key in ("base_head", "merge_commit"):
            if not isinstance(landed[key], str) or not SHA.fullmatch(landed[key]):
                err("landed", key + " must be a 40-hex commit sha")
        if landed["how"] not in ("ff", "merge", "union"):
            err("landed", "how must be one of ff, merge, union")
    spec = raw["spec"]
    if not isinstance(spec, dict):
        err("spec", "expected a dict")
    required = {"brief", "packet_id", "gates", "base"}
    missing = required - set(spec)
    unknown = set(spec) - (required | {"max_rounds"})
    if missing:
        err("spec", "missing keys: " + ", ".join(sorted(missing)))
    if unknown:
        err("spec", "unknown keys: " + ", ".join(sorted(unknown)))
    if spec["brief"] != raw["brief"]:
        err("spec", "brief must be the task's own brief file")
    if not isinstance(spec["packet_id"], str) or not PACKET_ID.fullmatch(spec["packet_id"]):
        err("spec", "packet_id must be a brief heading id like A1")
    base = spec["base"]
    if not isinstance(base, str) or not BASE_REF.fullmatch(base) or ".." in base or len(base) > 80:
        err("spec", "base must be a branch or ref name in the project repository")
    rounds = spec.get("max_rounds", 3)
    if isinstance(rounds, bool) or not isinstance(rounds, int) or not 1 <= rounds <= 8:
        err("spec", "max_rounds must be an int in 1..8")
    gates = spec["gates"]
    if not isinstance(gates, list) or not gates:
        err("spec", "gates must be a non-empty list")
    for gate in gates:
        if not isinstance(gate, dict) or not {"name", "argv"} <= set(gate):
            err("spec", "each gate needs a name and argv")
        unknown = set(gate) - {"name", "argv", "cwd", "timeout", "env"}
        if unknown:
            err("spec", "unknown gate keys: " + ", ".join(sorted(unknown)))
        if not isinstance(gate["name"], str) or not gate["name"] or len(gate["name"]) > 80:
            err("spec", "gate name must be a non-empty str of at most 80 chars")
        argv = gate["argv"]
        if not isinstance(argv, list) or not argv or not all(isinstance(x, str) for x in argv):
            err("spec", "gate argv must be a non-empty list of strs")
        if "cwd" in gate and (not isinstance(gate["cwd"], str) or not gate["cwd"]):
            err("spec", "gate cwd must be a non-empty str")
        if "timeout" in gate and (
            isinstance(gate["timeout"], bool)
            or not isinstance(gate["timeout"], int)
            or not 1 <= gate["timeout"] <= 3600
        ):
            err("spec", "gate timeout must be an int in 1..3600")
        if "env" in gate and (
            not isinstance(gate["env"], dict)
            or not all(isinstance(k, str) and isinstance(v, str) for k, v in gate["env"].items())
        ):
            err("spec", "gate env must be a dict of strs")
    out = {
        k: (dict(v) if k == "budget" else list(v) if isinstance(v, list) else v)
        for k, v in raw.items()
        if k not in ("spec", "landed", "failover", "failover_from")
    }
    out["failover"] = failover
    if failover_from is not None:
        out["failover_from"] = failover_from
    out["spec"] = {
        "brief": spec["brief"],
        "packet_id": spec["packet_id"],
        "gates": [dict(g) for g in gates],
        "base": base,
        "max_rounds": rounds,
    }
    if landed is not None:
        out["landed"] = dict(landed)
    return out


def packet_adapter(kind, lane, work, attempt_dir, session_name):
    """The packet adapter for a CLI lane kind, or Refused when the kind cannot re-enter."""
    if kind in UNSUPPORTED_ADAPTERS:
        raise Refused("packet lane kind " + kind + " unsupported: " + UNSUPPORTED_ADAPTERS[kind])
    if kind == "goat_cli":
        # The effort module lives beside the attempt, never in the worktree: an untracked
        # file would trip the commit gate's clean-tree check.
        mod = Path(attempt_dir) / EFFORT_MODULE_NAME
        mod.write_text(EFFORT_MODULE_TEXT)
        return CommandCodeAdapter(
            lane["model"], mod_path=mod, binary=lane["executable"], session_name=session_name
        )
    if kind == "zcode_cli":
        return ZcodeAdapter(work=work, wrapper=lane["executable"])
    if kind == "cline_cli":
        # The CLI's login lives in its data directory; the pass entitlement follows it, so
        # the packet runs on the operator's own Cline state (readable, digested like GOAT's).
        return ClineAdapter(
            lane["model"],
            work=work,
            data_dir=Path.home() / ".cline" / "data",
            binary=lane["executable"],
        )
    raise Refused("lane kind " + kind + " does not run packet tasks")


def build_sandbox(kind, work, attempt_dir):
    """The workspace write sandbox around the agent; the CLI's own login stays readable."""
    from ..lanes import sandbox

    home = Path.home()
    extra = home / {"goat_cli": ".commandcode", "cline_cli": ".cline"}.get(kind, ".zcode")
    # The agent writes only in its clone, its CLI's own state and a scratch tmp/ under the
    # attempt; the attempt directory itself is the harness's (transcripts, gates), written
    # from outside the sandbox. Granting all of it would put the clone's parent inside a
    # writable root and defeat the isolation probe.
    tmp = Path(attempt_dir) / "tmp"
    tmp.mkdir(exist_ok=True)
    profile = sandbox.write_profile(
        work,
        Path(attempt_dir) / "packet.sb",
        extra_write_roots=[extra, tmp],
        deny_read_roots=sandbox.deny_read_roots(),
    )
    sandbox.probe(profile, work)
    return lambda argv: sandbox.command(profile, argv)


def _git(repo, *args, check=True):
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True)
    if check and proc.returncode != 0:
        raise RuntimeError(
            "git " + " ".join(args) + ": " + proc.stderr.decode("utf-8", "replace").strip()
        )
    return proc.stdout


def _gates_for(spec, base_rev, attempt_dir, trailer):
    """The task's declared gates, then the commit gate: one commit carrying the lane's
    own trailer (the model that did the work, never the GLM default), clean tree."""
    gates = [
        Gate(
            g["name"],
            list(g["argv"]),
            cwd=g.get("cwd", "."),
            timeout=g.get("timeout", 1800),
            env=dict(g.get("env") or {}),
        )
        for g in spec["gates"]
    ]
    gate_dir = Path(attempt_dir) / "gate-scripts"
    gate_dir.mkdir(parents=True, exist_ok=True)
    script = gate_dir / "commit_gate.py"
    script.write_text(commit_gate_script(base_rev, trailer))
    gates.append(Gate("commit", [sys.executable, str(script)], timeout=60))
    return gates


def _account_windows(ledger, account_alias):
    from ..ledger import accounts, aliases
    from sqlalchemy import select

    with ledger.engine.connect() as con:
        binding = con.execute(select(aliases).where(aliases.c.id == account_alias)).mappings().one()
        acct = (
            con.execute(select(accounts).where(accounts.c.id == binding["account"]))
            .mappings()
            .one()
        )
    return {window: 0.01 for window in acct["windows"]}


def admit_packet(ledger, lanes, lane_id, task, project_root, packet_dir, account_alias):
    """Admit one packet attempt (submit, claim, lease) without running the loop.

    The admission half of a packet attempt, split out so the board runner can admit on
    its pass loop and run the long build→gate→re-enter loop in a worker thread (J6). The
    base must resolve and the brief must parse before anything is admitted — those are
    task-authoring errors the operator fixes, not attempts to hold.

    Returns the admission `run_packet` consumes: `aid` and `generation`, plus either the
    `context` the loop needs or the `state` a reconciliation hold inside `start` produced
    (a held attempt is never run, and its ledger reason stands).
    """
    lane = lanes[lane_id]
    kind = lane["kind"]
    if kind in UNSUPPORTED_ADAPTERS:
        raise Refused("packet lane kind " + kind + " unsupported: " + UNSUPPORTED_ADAPTERS[kind])
    if kind not in PACKET_KINDS:
        raise Refused("lane kind " + kind + " does not run packet tasks")
    spec = task["spec"]
    project_root = Path(project_root)
    branch = "packet/" + task["id"]
    try:
        base_rev = _git(project_root, "rev-parse", "--verify", spec["base"] + "^{commit}")[
            :40
        ].decode()
    except RuntimeError as exc:
        raise Refused("packet base not found: " + str(exc)[:160]) from None
    brief_doc = (project_root / spec["brief"]).read_text()
    try:
        packet = packet_text(brief_doc, spec["packet_id"])
        rules = hard_rules(brief_doc)
    except (KeyError, ValueError) as exc:
        raise Refused("packet brief unusable: " + str(exc)[:160]) from None

    workspace = Path(packet_dir) / "attempts"
    workspace.mkdir(parents=True, exist_ok=True)
    task_id = (
        task["id"]
        + "-"
        + time.strftime("%Y%m%dT%H%M%S", time.gmtime(time.time()))
        + "-"
        + uuid.uuid4().hex[:6]
    )
    ledger_spec = {
        "authorized": True,
        "model": lane["model"],
        "family": lane["family"],
        # Descriptive: the loop builds one argv per round from the adapter.
        "argv": ["packet-loop:" + kind, spec["packet_id"], task["id"]],
        "workspace": str(workspace),
        "timeout": min(task["budget"]["wall_seconds"] + 40, 3600),
        "output_bytes": task["budget"]["output_bytes"],
        "inputs": {},
        "manifest_sha256": digest({}),
    }
    ledger.submit(task_id, project_root.name, ledger_spec)
    aid, generation = ledger.claim(task_id, account_alias, _account_windows(ledger, account_alias))
    if ledger.start(aid, generation) is None:
        # Admission reconciliation held it inside start; the ledger reason stands.
        state = next((r["state"] for r in ledger.status() if r["id"] == aid), "held")
        return {"aid": aid, "generation": generation, "state": state, "context": None}

    attempt_dir = workspace / aid
    attempt_dir.mkdir(mode=0o700)
    return {
        "aid": aid,
        "generation": generation,
        "state": None,
        "context": {
            "lane": lane,
            "kind": kind,
            "task": task,
            "spec": spec,
            "branch": branch,
            "base_rev": base_rev,
            "packet": packet,
            "rules": rules,
            "project_root": project_root,
            "attempt_dir": attempt_dir,
        },
    }


def checkout_bytes(project_root, rev):
    """Bytes a full checkout of `rev` writes: the sum of its blob sizes, read from the tree
    alone. A shared clone saves the objects but not the working tree, so this is what one
    more attempt costs the packets volume (vix-rs: 3.2 GiB of committed series per clone)."""
    total = 0
    for line in _git(project_root, "ls-tree", "-r", "-l", rev).decode().splitlines():
        meta = line.split("\t", 1)[0].split()
        if len(meta) == 4 and meta[1] == "blob":
            total += int(meta[3])
    return total


RESERVATIONS = ".reservations"
# A clone that has not finished in an hour is not coming; its reservation is ignored.
RESERVATION_TTL_SECONDS = 3600


def _packets_root(attempt_dir):
    """The packets root above an attempt: <packets_root>/<task>/<stamp>/attempts/<aid>."""
    return Path(attempt_dir).parents[3]


def reserved_bytes(packets_root, now=None):
    """Bytes other attempts have claimed for clones still being written.

    Concurrent dispatch (J6) runs several boards as separate processes and several
    packets per board as threads, so a free-space reading is stale the moment two of
    them take it: on 2026-09-16 two 3.2 GiB vix-rs clones each passed the pre-clone
    guard against the same 15 GiB and left 5 GiB. Each pending clone writes its size to
    `<packets_root>/.reservations/<aid>` before cloning and removes it after, and the
    guard subtracts the live reservations from what the volume reports."""
    now = time.time() if now is None else now
    root = Path(packets_root) / RESERVATIONS
    total = 0
    if not root.is_dir():
        return 0
    for entry in root.iterdir():
        try:
            if now - entry.stat().st_mtime > RESERVATION_TTL_SECONDS:
                continue
            total += int(entry.read_text().strip() or 0)
        except (OSError, ValueError):
            continue
    return total


def _reserve(packets_root, aid, nbytes):
    root = Path(packets_root) / RESERVATIONS
    root.mkdir(parents=True, exist_ok=True)
    (root / aid).write_text(str(nbytes) + "\n")
    return root / aid


def run_packet(ledger, admission, *, min_free_bytes=DEFAULT_MIN_FREE_BYTES, free_bytes=None):
    """Run an admitted packet attempt's loop and settle it; returns (aid, state, attempt_dir).

    The loop half of a packet attempt: build the scratch clone, run the lane adapter with
    the declared gates and the commit gate, and either fetch the branch and complete the
    attempt with the verdict as its receipt, or hold it with the loop's reason and leave
    the branch for the operator. A reconciliation hold at admission returns immediately
    without running anything, the state `admit_packet` reported.

    The clone is the expensive part, so the disk guard runs before it, not just before
    each round: the volume must hold `min_free_bytes` plus the checkout itself, or the
    attempt is held `disk_low` without writing anything. Concurrent dispatch (J6) starts
    several clones in one tick, so the floor alone is not enough — on 2026-09-16 four of
    them filled the disk together. On completion the clone is removed: the branch is
    already fetched into the project and the receipt pins its head, so the working tree
    is dead weight (30 of 37 GiB of packet workspaces were landed clones).
    """
    aid = admission["aid"]
    context = admission["context"]
    if context is None:
        return aid, admission["state"], None
    generation = admission["generation"]
    lane = context["lane"]
    kind = context["kind"]
    task = context["task"]
    spec = context["spec"]
    branch = context["branch"]
    base_rev = context["base_rev"]
    project_root = context["project_root"]
    attempt_dir = context["attempt_dir"]
    measure = free_bytes or free_disk_bytes
    reservation = None
    try:
        attempt_dir.mkdir(parents=True, exist_ok=True)
        packets_root = _packets_root(attempt_dir)
        checkout = checkout_bytes(project_root, base_rev)
        # Claim first, then read: a reader who claims after us sees our reservation, and
        # ours counts the claims that beat us to the directory.
        reservation = _reserve(packets_root, aid, checkout)
        free = measure(attempt_dir) - (reserved_bytes(packets_root) - checkout)
        need = min_free_bytes + checkout
        if free < need:
            reservation.unlink(missing_ok=True)
            reservation = None
            ledger.hold(
                aid,
                f"packet attempt: disk_low: {free} free after other pending clones, "
                f"clone needs {need}",
            )
            return aid, "held", attempt_dir
        clone = attempt_dir / "work"
        _git(project_root, "clone", "-q", "--shared", str(project_root), str(clone))
        # The checkout is on disk now; what the volume reports includes it.
        reservation.unlink(missing_ok=True)
        reservation = None
        _git(clone, "checkout", "-q", "-B", branch, base_rev)
        adapter = packet_adapter(
            kind, lane, clone, attempt_dir, session_name=f"packet-{task['id']}-{aid[:8]}"
        )
        sandbox_command = build_sandbox(kind, clone, attempt_dir)
        env = dict(
            os.environ, HOME=str(Path.home()), TMPDIR=str(attempt_dir / "tmp"), PYTHONPATH="src"
        )
        (attempt_dir / "tmp").mkdir(exist_ok=True)
        prompt = compose_prompt(
            context["rules"],
            orient(clone, mentioned_paths(context["packet"])),
            context["packet"],
            branch=branch,
            base=spec["base"],
            python=sys.executable,
            report_name=task["id"] + ".md",
            trailer=trailer_for(lane["model"]),
        )
        verdict = build_loop(
            adapter,
            sandbox_command,
            clone,
            env,
            prompt,
            _gates_for(spec, base_rev, attempt_dir, trailer_for(lane["model"])),
            attempt_dir,
            wall_seconds=task["budget"]["wall_seconds"],
            max_rounds=spec["max_rounds"],
        )
    except Exception as exc:
        if reservation is not None:
            reservation.unlink(missing_ok=True)
        ledger.hold(aid, "packet attempt: " + type(exc).__name__ + ": " + str(exc)[:300])
        return aid, "held", attempt_dir

    rounds = len(verdict.get("rounds") or [])
    repairs = max(0, rounds - 1)
    if not verdict.get("gates_passed"):
        ledger.hold(aid, "packet loop: " + str(verdict.get("reason")))
        return aid, "held", attempt_dir
    clone = attempt_dir / "work"
    head = _git(clone, "rev-parse", "HEAD").decode().strip()
    # The deliverable is the branch; its receipt artifact pins the exact commit object.
    commit_digest = hashlib.sha256(_git(clone, "cat-file", "commit", head)).hexdigest()
    _git(project_root, "fetch", "-q", str(clone), f"refs/heads/{branch}:refs/heads/{branch}")
    # The branch now lives in the project; the clone's working tree is reclaimed. Held
    # attempts keep theirs — verify_followup reads the files a hold left behind.
    shutil.rmtree(clone, ignore_errors=True)
    receipt = {
        "status": "completed",
        "finish_reason": "stop",
        "actual_model": lane["model"],
        "manifest_sha256": digest({}),
        "artifacts": [{"path": branch, "sha256": commit_digest}],
        "verified_in_lane": bool(verdict.get("verified_in_lane")),
        # The final round's gate results, so the review policy can read what the receipt
        # claims was proved instead of trusting a single boolean.
        "gates": [
            {"name": r.get("name"), "ok": r.get("ok")}
            for r in (verdict.get("rounds") or [{}])[-1].get("results") or []
        ],
        "repairs": repairs,
        "branch": branch,
        "head": head,
        "rounds": rounds,
        "loop_reason": verdict.get("reason"),
        "session_id": verdict.get("session_id"),
    }
    ledger.finish(aid, generation, receipt)
    ledger.record_outcome(
        aid,
        "packet",
        True,
        repairs=repairs,
        note=f"packet {spec['packet_id']}: gates green in {rounds} round(s); branch at {head[:7]}",
    )
    return aid, "completed", attempt_dir
