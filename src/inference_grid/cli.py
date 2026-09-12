import argparse
import json
import os

from .collector import refresh_collect
from .ledger import Ledger
from .queue import hold_abandoned, publish
from .scheduler import tick
from .worker import execute


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
            "tick",
        ],
    )
    parser.add_argument("--json", help="JSON argument file; never store credentials here")
    args = parser.parse_args()
    if args.command == "doctor":
        from .doctor import diagnose

        report = diagnose(args.database)
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
            "defer",
            "cooldown",
        }
        and not args.json
    ):
        parser.error("this command requires --json")
    ledger = Ledger(args.database)
    data = json.load(open(args.json)) if args.json else {}
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
    }
    print(json.dumps(commands[args.command](**data), indent=2))


if __name__ == "__main__":
    main()
