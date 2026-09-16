import argparse
import json
import os
from pathlib import Path

from .collector import refresh_collect
from .ledger import Ledger
from .queue import hold_abandoned, publish
from .scheduler import tick
from .worker import execute


def board_tick(
    ledger,
    board_dir=None,
    project_root=None,
    lanes_path=None,
    accounts_by_lane=None,
    packets_root=None,
    prepare_argv=None,
    dry_run=False,
    boards=None,
    auto_land=False,
    auto_dispatch=False,
    observations=None,
    output_dir=None,
    package_src=None,
    owner_only=None,
    require_lane_meta=None,
):
    from .board.runner import tick as board_run
    from .lanes.runner import load_lanes

    if boards is not None:
        # Several boards, one call, ticked in order. Readiness comes from the ledger at
        # each tick's start (and after every dispatch within it), so busy counts carry
        # across boards: a lane busy on board A is busy on board B without a re-dispatch.
        return [
            {
                "board": config.get("board_dir"),
                "results": board_tick(
                    ledger,
                    dry_run=dry_run,
                    prepare_argv=config.get("prepare_argv", prepare_argv),
                    **{
                        key: config[key]
                        for key in (
                            "board_dir",
                            "project_root",
                            "lanes_path",
                            "accounts_by_lane",
                            "packets_root",
                            "auto_land",
                            "auto_dispatch",
                            "observations",
                            "output_dir",
                            "package_src",
                            "owner_only",
                            "require_lane_meta",
                        )
                        if key in config
                    },
                ),
            }
            for config in boards
        ]

    return board_run(
        board_dir,
        project_root,
        ledger,
        load_lanes(lanes_path),
        lanes_path,
        accounts_by_lane,
        packets_root,
        prepare_argv=prepare_argv,
        dry_run=dry_run,
        auto_land=auto_land,
        auto_dispatch=auto_dispatch,
        observations=observations,
        output_dir=output_dir,
        package_src=package_src,
        owner_only=owner_only,
        require_lane_meta=require_lane_meta,
    )


def board_new(
    board_dir,
    project_root=None,
    task=None,
    retry=None,
    change=None,
    budget=None,
    lanes=None,
    author_family=None,
    review_branch=None,
    max_input_bytes=None,
    split=None,
    include_docs=None,
    exclude_commits=None,
    qualify=None,
    allowed_prefixes=None,
    ticket=None,
    lane=None,
):
    from .board.new import new_task, retry_task

    if ticket is not None:
        from .board.plan_task import plan_task

        if not lane:
            raise ValueError("board-new from a ticket needs the lane to plan on")
        return plan_task(board_dir, ticket, lane)
    if project_root is None:
        raise ValueError("board-new needs project_root")
    if qualify is not None:
        from .board.new import qualify_task

        return qualify_task(board_dir, project_root, qualify["lane_id"], qualify["category"])
    if review_branch is not None:
        from .board.branch_review import review_branch as author_review_branch

        return author_review_branch(
            board_dir,
            project_root,
            review_branch,
            lanes=lanes,
            budget=budget,
            max_input_bytes=max_input_bytes,
            split=split,
            include_docs=include_docs,
            exclude_commits=exclude_commits,
            allowed_prefixes=allowed_prefixes,
        )
    if retry is not None:
        return retry_task(
            board_dir,
            project_root,
            retry,
            change,
            budget=budget,
            lanes=lanes,
            author_family=author_family,
        )
    if task is None:
        raise ValueError("board-new needs either a task or a retry")
    return new_task(board_dir, project_root, task)


def main():
    parser = argparse.ArgumentParser(description="Durable admission for trusted, bounded adapters")
    parser.add_argument(
        "--database",
        default=os.environ.get("GRID_DATABASE_URL", "sqlite:///grid.sqlite"),
    )
    parser.add_argument(
        "command",
        choices=[
            "init",
            "doctor",
            "defer",
            "cooldown",
            "account",
            "submit",
            "claim",
            "status",
            "run",
            "publish",
            "hold-abandoned",
            "accept",
            "resolve",
            "refresh-collect",
            "lane",
            "outcome",
            "external",
            "scorecard",
            "board-tick",
            "board-new",
            "board-status",
            "inbox-integrate",
            "lane-init",
            "board-init",
            "calibrate",
            "calibration-score",
            "evals",
            "tick-all",
            "digest",
            "boards",
            "evaluation",
            "tick",
            "watch",
            "verify-merge",
            "land",
        ],
    )
    parser.add_argument("--json", help="JSON argument file; never store credentials here")
    args = parser.parse_args()
    if args.command == "doctor":
        from .doctor import diagnose

        extra = json.load(open(args.json)) if args.json else {}
        boards = extra.get("boards") if isinstance(extra, dict) else None
        lanes_path = extra.get("lanes_path") if isinstance(extra, dict) else None
        lane_specs = None
        if lanes_path:
            from .lanes.runner import load_lanes

            lane_specs = load_lanes(lanes_path)
        report = diagnose(args.database, boards=boards, lane_specs=lane_specs)
        print(json.dumps(report, indent=2))
        raise SystemExit(0 if report["status"] == "checks_passed" else 1)
    if (
        args.command
        in {
            "account",
            "submit",
            "claim",
            "run",
            "hold-abandoned",
            "accept",
            "resolve",
            "refresh-collect",
            "lane",
            "outcome",
            "external",
            "board-tick",
            "board-new",
            "board-status",
            "inbox-integrate",
            "lane-init",
            "board-init",
            "calibrate",
            "calibration-score",
            "evals",
            "defer",
            "cooldown",
            "watch",
            "verify-merge",
            "land",
            "boards",
        }
        and not args.json
    ):
        parser.error("this command requires --json")
    if args.command == "verify-merge":
        # No ledger, no database, no quota: a code node over a git worktree.
        from .board.verify_merge import verify_merge_cli

        report = verify_merge_cli(json.load(open(args.json)))
        print(json.dumps(report, indent=2))
        raise SystemExit(0 if report["mergeable"] and all(g["ok"] for g in report["gates"]) else 1)
    ledger = Ledger(args.database)
    data = json.load(open(args.json)) if args.json else {}
    if args.command == "evaluation":
        # Markdown, not JSON: stdout by default, the out file otherwise. With
        # replace_section the generated section is spliced into the file — the one
        # sanctioned docs/ write — leaving every other line of it untouched.
        from .evaluation import evaluation_document, write_document, write_section

        document = evaluation_document(ledger)
        out = data.get("out") if isinstance(data, dict) else None
        section = data.get("replace_section") if isinstance(data, dict) else None
        if out and section:
            path = write_section(document, out, section)
            print(json.dumps({"wrote": str(path), "section": section}, indent=2))
        elif out:
            path = write_document(document, out)
            print(json.dumps({"wrote": str(path), "bytes": len(document)}, indent=2))
        else:
            print(document, end="")
        return
    commands = {
        "init": ledger.initialize,
        "defer": ledger.defer,
        "cooldown": ledger.cooldown_status,
        "account": ledger.configure_account,
        "tick": lambda: tick(ledger),
        "submit": ledger.submit,
        "claim": ledger.claim,
        "status": ledger.status,
        "run": lambda **kw: execute(ledger, **kw),
        "publish": lambda: publish(ledger),
        "hold-abandoned": lambda **kw: hold_abandoned(ledger, **kw),
        "accept": ledger.accept,
        "resolve": ledger.resolve,
        "refresh-collect": lambda **kw: refresh_collect(ledger, kw),
        "lane": ledger.record_lane,
        "outcome": ledger.record_outcome,
        "external": ledger.record_external,
        "scorecard": ledger.scorecard,
        "board-tick": lambda **kw: board_tick(ledger, **kw),
        "board-new": lambda **kw: board_new(**kw),
        "calibrate": lambda **kw: calibrate(**kw),
        "calibration-score": lambda **kw: calibration_score(ledger, **kw),
        "evals": lambda **kw: evals(ledger, **kw),
        "board-status": lambda **kw: board_status(ledger, **kw),
        "lane-init": lambda **kw: lane_init(**kw),
        "board-init": lambda **kw: board_init(**kw),
        "tick-all": lambda **kw: tick_all(ledger, **kw),
        "digest": lambda **kw: digest(ledger, **kw),
        "boards": lambda **kw: boards_command(ledger, **kw),
        "watch": lambda **kw: watch(kw),
        "inbox-integrate": lambda **kw: inbox_integrate(
            kw["project_root"], kw["task_id"], dry_run=kw.get("dry_run", True)
        ),
        "land": lambda **kw: land_command(ledger, **kw),
    }
    report = commands[args.command](**data)
    if args.command == "land":
        # A landing that refused or a dry run that would not land is worth a non-zero exit;
        # a dry run that would land exits 0 without having touched anything.
        ok = bool(report.get("landed") or report.get("would_land"))
        print(json.dumps(report, indent=2))
        raise SystemExit(0 if ok else 1)
    print(json.dumps(report, indent=2))


def inbox_integrate(project_root, task_id, dry_run=True):
    from .board.integrate import inbox_integrate as integrate

    return integrate(project_root, task_id, dry_run=dry_run)


def land_command(ledger, **kw):
    from .board.land import land

    return land(ledger=ledger, **kw)


def board_status(ledger, board_dir=None, suggest=False, boards=None):
    from .board.status import board_status as status

    return status(ledger, board_dir, suggest=suggest, boards=boards)


def calibrate(board_dir, project_root, corpus_dir, lanes, run_id):
    from .board.calibration import author_calibration

    return author_calibration(board_dir, project_root, corpus_dir, lanes, run_id)


def calibration_score(ledger, board_dir, run_id, packets_root, record=False):
    from .board.calibration import score_calibration

    return score_calibration(board_dir, run_id, packets_root, record=record, ledger=ledger)


def evals(ledger, corpus_dir, lanes, board_dir, project_root, every_days=None):
    """Author the calibration_run tasks every lane's stale evals are due.

    `lanes` is the operator's lanes.json path (the lane set the freshness is measured
    against); `project_root` is the board's project, where the runner stages a case's
    review task — the authoring itself writes under `board_dir` and its grid root. The
    command is idempotent per day and per (lane, case), so an hourly launchd job is safe.
    """
    from .board.evals import EVAL_EVERY_DAYS, author_evals
    from .lanes.runner import load_lanes

    if not corpus_dir or not board_dir or not project_root or not lanes:
        raise ValueError("evals needs corpus_dir, lanes, board_dir and project_root")
    lane_specs = load_lanes(lanes) if isinstance(lanes, (str, os.PathLike)) else lanes
    authored = author_evals(
        board_dir,
        corpus_dir,
        lane_specs,
        ledger,
        every_days=every_days or EVAL_EVERY_DAYS,
    )
    return {"project_root": str(project_root), "authored": authored}


def lane_init(lane_id, board_dir, project_root=None):
    from .board.new import lane_init as init

    root = project_root or str(Path(board_dir).parent.parent)
    return init(board_dir, root, lane_id)


def board_init(project_root, board_name=None, allowed_prefixes=None, tier="T0"):
    from .board.new import board_init as init

    return init(project_root, board_name=board_name, allowed_prefixes=allowed_prefixes, tier=tier)


def tick_all(ledger, boards_dir, log_path=None, prepare=None, dry_run=False):
    from .tick_all import tick_all as run_all

    return run_all(ledger, boards_dir, log_path=log_path, prepare=prepare, dry_run=dry_run)


def digest(ledger, boards_dir, since=None):
    from .digest import digest as render

    return render(ledger, boards_dir, now=since)


def boards_command(
    ledger, boards_dir=None, boards=None, database=None, packets_root=None, now=None
):
    from .boards import boards as render

    return render(
        ledger,
        boards_dir=boards_dir,
        boards=boards,
        database=database,
        packets_root=packets_root,
        now=now,
    )


def watch(spec):
    from .watch import watch as run_watch

    return run_watch(spec)


if __name__ == "__main__":
    main()
