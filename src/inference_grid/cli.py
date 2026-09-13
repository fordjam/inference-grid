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
    board_dir,
    project_root,
    lanes_path,
    accounts_by_lane,
    packets_root,
    prepare_argv=None,
    dry_run=False,
):
    from .board.runner import tick as board_run
    from .lanes.runner import load_lanes

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
):
    from .board.new import new_task, retry_task

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
            "defer",
            "cooldown",
        }
        and not args.json
    ):
        parser.error("this command requires --json")
    ledger = Ledger(args.database)
    data = json.load(open(args.json)) if args.json else {}
    if args.command == "evaluation":
        # Markdown, not JSON: the document goes to stdout, or to out (never into docs/).
        from .evaluation import evaluation_document, write_document

        document = evaluation_document(ledger)
        out = data.get("out") if isinstance(data, dict) else None
        if out:
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
    }
    print(json.dumps(commands[args.command](**data), indent=2))


def board_status(ledger, board_dir, suggest=False):
    from .board.status import board_status as status

    return status(ledger, board_dir, suggest=suggest)


if __name__ == "__main__":
    main()
