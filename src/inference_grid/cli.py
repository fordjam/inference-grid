import argparse
import json
import os

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
    )


def board_new(
    board_dir,
    project_root,
    task=None,
    retry=None,
    change=None,
    budget=None,
    lanes=None,
    author_family=None,
    review_branch=None,
    max_input_bytes=None,
    split=None,
):
    from .board.new import new_task, retry_task

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
            "scorecard",
            "board-tick",
            "board-new",
            "board-status",
            "inbox-integrate",
            "evaluation",
            "tick",
        ],
    )
    parser.add_argument("--json", help="JSON argument file; never store credentials here")
    args = parser.parse_args()
    if args.command == "doctor":
        from .doctor import diagnose

        extra = json.load(open(args.json)) if args.json else {}
        boards = extra.get("boards") if isinstance(extra, dict) else None
        report = diagnose(args.database, boards=boards)
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
            "board-tick",
            "board-new",
            "board-status",
            "inbox-integrate",
            "defer",
            "cooldown",
        }
        and not args.json
    ):
        parser.error("this command requires --json")
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
        "scorecard": ledger.scorecard,
        "board-tick": lambda **kw: board_tick(ledger, **kw),
        "board-new": lambda **kw: board_new(**kw),
        "board-status": lambda **kw: board_status(ledger, **kw),
        "inbox-integrate": lambda **kw: inbox_integrate(
            kw["project_root"], kw["task_id"], dry_run=kw.get("dry_run", True)
        ),
    }
    print(json.dumps(commands[args.command](**data), indent=2))


def board_status(ledger, board_dir=None, suggest=False, boards=None):
    from .board.status import board_status as status

    return status(ledger, board_dir, suggest=suggest, boards=boards)


def inbox_integrate(project_root, task_id, dry_run=True):
    from .board.integrate import inbox_integrate as integrate

    return integrate(project_root, task_id, dry_run=dry_run)


if __name__ == "__main__":
    main()
