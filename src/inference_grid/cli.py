import argparse
import json
import time
import os
from pathlib import Path

from .collector import refresh_collect
from .ledger import Ledger, hold_abandoned
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
    from_brief=None,
    gates=None,
    base=None,
    boards_dir=None,
    dry_run=False,
):
    from .board.new import new_task, retry_task

    if ticket is not None:
        from .board.plan_task import plan_task

        if not lane:
            raise ValueError("board-new from a ticket needs the lane to plan on")
        return plan_task(board_dir, ticket, lane)
    if project_root is None:
        raise ValueError("board-new needs project_root")
    if from_brief is not None:
        from .board.from_brief import from_brief as author_from_brief

        return author_from_brief(
            board_dir,
            project_root,
            from_brief,
            lanes=lanes,
            gates=gates,
            base=base,
            boards_dir=boards_dir,
            dry_run=dry_run,
        )
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
            "calibration-score",
            "digest",
            "boards",
            "evaluation",
            "report",
            "state-migrate",
            "tick",
            "watch",
            "verify-merge",
            "land",
            "needs-you",
        ],
    )
    parser.add_argument("--json", help="JSON argument file; never store credentials here")
    parser.add_argument(
        "--week", help="report only: an ISO week (2026-W38); default the last 7 days"
    )
    parser.add_argument("--repo", help="state-migrate only: the product repo's path")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="state-migrate only: required tonight (B6) — no mover exists yet",
    )
    parser.add_argument(
        "--board",
        help="state-migrate only: explicit board/destination name, "
        "required when the auto-derived state/<repo-name>/ already exists",
    )
    args = parser.parse_args()
    if args.command != "state-migrate" and (args.repo or args.dry_run or args.board):
        parser.error("--repo/--dry-run/--board are state-migrate only")
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
            "calibration-score",
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
    if args.command == "state-migrate":
        # No ledger: a read-only plan over the repo's own filesystem.
        from .state_migrate import state_migrate

        if not args.repo:
            parser.error("state-migrate requires --repo")
        plan = state_migrate(args.repo, dry_run=args.dry_run, board=args.board)
        print(json.dumps(plan, indent=2))
        return
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
    if args.command == "report":
        # 02-C1: markdown, always to stdout *and* to the log path (config `out`,
        # default ~/Library/Logs/inference-grid/report-<week>.md) — both, not either/or.
        document, _path = report_command(
            ledger,
            week=args.week,
            lanes=data.get("lanes"),
            accounts_by_lane=data.get("accounts_by_lane"),
            prices=data.get("prices"),
            subscriptions=data.get("subscriptions"),
            quota_use=data.get("quota_use"),
            out=data.get("out"),
        )
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
        "calibration-score": lambda **kw: calibration_score(ledger, **kw),
        "board-status": lambda **kw: board_status(ledger, **kw),
        "lane-init": lambda **kw: lane_init(**kw),
        "board-init": lambda **kw: board_init(**kw),
        "digest": lambda **kw: digest(ledger, **kw),
        "boards": lambda **kw: boards_command(ledger, **kw),
        "watch": lambda **kw: watch(kw),
        "needs-you": lambda **kw: needs_you_command(ledger, **kw),
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


def calibration_score(ledger, board_dir, run_id, packets_root, record=False):
    from .board.calibration import score_calibration

    return score_calibration(board_dir, run_id, packets_root, record=record, ledger=ledger)


def lane_init(lane_id, board_dir, project_root=None):
    from .board.new import lane_init as init

    root = project_root or str(Path(board_dir).parent.parent)
    return init(board_dir, root, lane_id)


def board_init(project_root, board_name=None, allowed_prefixes=None, tier="T0"):
    from .board.new import board_init as init

    return init(project_root, board_name=board_name, allowed_prefixes=allowed_prefixes, tier=tier)


def digest(ledger, boards_dir, since=None):
    from .digest import digest as render

    return render(ledger, boards_dir, now=since)


DEFAULT_REPORT_LOG_DIR = Path.home() / "Library/Logs/inference-grid"
DEFAULT_PRICES_PATH = Path.home() / ".config/inference-grid/prices.json"
DEFAULT_SUBSCRIPTIONS_PATH = Path.home() / ".config/inference-grid/subscriptions.json"
DEFAULT_CAPACITY_OBSERVATIONS_DIR = Path.home() / ".local/share/inference-grid-capacity"
WEEKS_PER_MONTH = 52 / 12

# 02-C5: the $34/month grid budget (Z.ai + Go + GOAT, 02-inference-grid.md's own
# Intent line) plus Codex (net $0 after the Amex credit — 02-inference-grid.md A10).
# "Max" is the operator's own Claude plan; its price is deliberately absent here, same
# "no price on file" convention as `_load_prices` -- an operator who wants it counted
# adds it to `~/.config/inference-grid/subscriptions.json`, never a guess baked in here.
# `account` is the ledger's own account id/alias (see aliases table), or a list of them
# when one subscription funds more than one account -- the Z.ai plan pays for both the
# `zai` and `zcode` lanes (deployments/local/board_prepare.py's own comment: "the Z.ai
# plan serves both the zai headless lane and the zcode lane"; both are real, separate
# ledger accounts -- `zai-account` and `zcode-account` -- not aliases of each other, per
# the ledger's own `aliases` table). Max has no ledger account at all: the operator's
# Claude Max login is `claude_headless`/provider `claude` (docs/LANES.md's "First-party
# Claude" section), explicit-only for reviews, and never appears in the `accounts`
# table -- `account: None` here, never a guess at which lane stands in for it. Codex
# likewise has none -- it isn't dispatched through this board, so its landed-packet
# count is always 0.
DEFAULT_SUBSCRIPTIONS_MONTHLY = {
    "Max": {"account": None, "monthly_cost_usd": None},
    "Z.ai": {"account": ["zai", "zcode"], "monthly_cost_usd": 14.0},
    "Go": {"account": "go", "monthly_cost_usd": 10.0},
    "GOAT": {"account": "goat-account", "monthly_cost_usd": 10.0},
    "Codex": {"account": None, "monthly_cost_usd": 0.0},
}

# 02-C5: the observation file each collector already writes under
# DEFAULT_CAPACITY_OBSERVATIONS_DIR (deployments/local/collect_*.py) -- no path invented
# beyond what those scripts use themselves. "Max" has no provider-reported quota API, so
# it carries no entry and `_load_quota_use` never reports on it.
QUOTA_OBSERVATION_FILES = {
    "Z.ai": "zai-observation.json",
    "Go": "go-live-observation.json",
    "GOAT": "goat-observation.json",
    "Codex": "codex-observation.json",
}


def _load_prices(explicit):
    """`prices` from the `--json` arg file when given; otherwise the operator's own
    `~/.config/inference-grid/prices.json` if present -- read-only, never created or
    written here. Shape: `{"<account or alias>": <USD monthly price>, ...}`. A file
    that parses but isn't that shape (a list, a number, ...) degrades to no prices,
    same as a missing or unparseable one -- report()'s own `prices.items()` would
    otherwise crash the whole command over one malformed config file."""
    if explicit is not None:
        return explicit
    try:
        loaded = json.load(open(DEFAULT_PRICES_PATH))
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _load_subscriptions(explicit):
    """`report()`'s `subscriptions` argument (already weekly-costed) from the `--json`
    arg file when given -- taken verbatim, since an explicit value is already in
    `report()`'s own shape. Absent that, the operator's own
    `~/.config/inference-grid/subscriptions.json` if present (shape:
    `{"<name>": {"account": <id-or-alias-or-None-or-list-of-those>, "monthly_cost_usd":
    <number-or-None>}}` -- a list when one subscription funds more than one ledger
    account), else `DEFAULT_SUBSCRIPTIONS_MONTHLY` -- unlike `_load_prices`, C5 names a small fixed
    table of known subscriptions (02-inference-grid.md's own $34/month + Codex), so a
    missing file falls back to that table rather than to nothing. Either way, the
    monthly price on file is converted to a weekly one here -- `report()` only divides,
    it never converts units."""
    if explicit is not None:
        return explicit
    try:
        loaded = json.load(open(DEFAULT_SUBSCRIPTIONS_PATH))
    except (OSError, ValueError):
        loaded = None
    monthly = loaded if isinstance(loaded, dict) else DEFAULT_SUBSCRIPTIONS_MONTHLY
    subscriptions = {}
    for name, spec in monthly.items():
        if not isinstance(spec, dict):
            continue
        cost = spec.get("monthly_cost_usd")
        weekly = (
            cost / WEEKS_PER_MONTH
            if isinstance(cost, (int, float)) and not isinstance(cost, bool)
            else None
        )
        subscriptions[name] = {"account": spec.get("account"), "weekly_cost_usd": weekly}
    return subscriptions


def _load_quota_use(explicit, capacity_dir=None):
    """`report()`'s `quota_use` argument from the `--json` arg file when given; otherwise
    read straight from the collectors' own observation files under
    `DEFAULT_CAPACITY_OBSERVATIONS_DIR` (or `capacity_dir`, test-only). Only the
    "weekly" window is used -- C5 asks for this week's consumption, not the five-hour or
    monthly one. Z.ai's window carries raw units (`plan_units`/`remaining_units`); the
    others report only a provider-computed percentage, so `numerator`/`denominator` stay
    `None` for them rather than a guessed unit count. A missing, unreadable or
    unrecognizable file contributes no entry for that subscription, never a fabricated
    reading."""
    if explicit is not None:
        return explicit
    directory = Path(capacity_dir) if capacity_dir else DEFAULT_CAPACITY_OBSERVATIONS_DIR
    result = {}
    for name, filename in QUOTA_OBSERVATION_FILES.items():
        try:
            data = json.load(open(directory / filename))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        windows = data.get("windows")
        if not isinstance(windows, list):
            continue
        weekly = next(
            (w for w in windows if isinstance(w, dict) and w.get("id") == "weekly"), None
        )
        if weekly is None:
            continue
        used_percent = weekly.get("used_percent")
        if type(used_percent) not in (int, float) or isinstance(used_percent, bool):
            continue
        numerator = denominator = None
        plan_units, remaining_units = weekly.get("plan_units"), weekly.get("remaining_units")
        if (
            type(plan_units) in (int, float)
            and not isinstance(plan_units, bool)
            and type(remaining_units) in (int, float)
            and not isinstance(remaining_units, bool)
        ):
            numerator, denominator = plan_units - remaining_units, plan_units
        result[name] = {
            "used_percent": used_percent,
            "numerator": numerator,
            "denominator": denominator,
            "window": "weekly",
            "observed_at": data.get("observed_at"),
        }
    return result


def _report_week_label(rep):
    """The ISO week the report's window falls in, for the log filename -- even when
    `week` was not given (the default last-7-days window still names a real week).

    Labeled from `start`, never `end`: the window is the seven days *ending* now, so a
    run right at a week boundary (the Monday 07:00 scheduled report chief among them)
    has an `end` already in the new ISO week while every event in the report is from
    the week before it -- labeling from `end` would name the file one week ahead of
    the report it actually holds.
    """
    if rep["week"]:
        return rep["week"]
    from datetime import datetime, timezone

    year, week, _ = datetime.fromtimestamp(rep["start"], timezone.utc).isocalendar()
    return f"{year}-W{week:02d}"


def report_command(
    ledger,
    week=None,
    lanes=None,
    accounts_by_lane=None,
    prices=None,
    subscriptions=None,
    quota_use=None,
    out=None,
):
    """Render the weekly markdown report and write it to the log path; returns
    `(document, path)` so the CLI can print the same text it just wrote."""
    from .report import render_markdown
    from .report import report as render

    rep = render(
        ledger,
        week=week,
        lanes=lanes,
        accounts_by_lane=accounts_by_lane,
        prices=_load_prices(prices),
        subscriptions=_load_subscriptions(subscriptions),
        quota_use=_load_quota_use(quota_use),
    )
    document = render_markdown(rep)
    if out:
        path = Path(out).expanduser()
        if path.suffix != ".md":
            # `out` is operator-supplied config, not user input off a form, but a
            # weekly report is always markdown -- refusing anything else is a cheap
            # guard against a typo'd config path clobbering an unrelated file.
            raise ValueError(f"report out path must end in .md, got {path}")
    else:
        path = DEFAULT_REPORT_LOG_DIR / f"report-{_report_week_label(rep)}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(document)
    return document, path


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


def needs_you_command(
    ledger,
    board_dirs=(),
    watch_state=None,
    owner_decisions=None,
    heartbeat_paths=None,
    data_status_paths=None,
    workspace_root=None,
    workspace_status_path=None,
    workspace_cap_bytes=None,
    packets_root=None,
    collector_paths=None,
    disk_path=None,
    disk_alarm_bytes=None,
    disk_red_bytes=None,
    memory_gate_config_path=None,
    memory_amber_gb=None,
    this_week_subscriptions=None,
    this_week_quota_use=None,
):
    from .needs_you import DEFAULT_WORKSPACE_ROOT, needs_you as render

    # This command is run on demand, never on a scheduled subprocess timeout, so a
    # live walk of ~/.grid-workspaces (needs_you.disk_usage_row) is the right default
    # here even though it costs real time -- unlike the capacity dashboard's overlay
    # build, which must only ever read a cached reading (see overlay_build.py).
    if workspace_root is None and workspace_status_path is None:
        workspace_root = str(DEFAULT_WORKSPACE_ROOT)
    return render(
        ledger,
        board_dirs=board_dirs,
        watch_state=watch_state,
        owner_decisions=owner_decisions,
        heartbeat_paths=heartbeat_paths,
        data_status_paths=data_status_paths,
        workspace_root=workspace_root,
        workspace_status_path=workspace_status_path,
        workspace_cap_bytes=workspace_cap_bytes,
        packets_root=packets_root,
        collector_paths=collector_paths,
        disk_path=disk_path,
        disk_alarm_bytes=disk_alarm_bytes,
        disk_red_bytes=disk_red_bytes,
        memory_gate_config_path=memory_gate_config_path,
        memory_amber_gb=memory_amber_gb,
        this_week_subscriptions=_load_subscriptions(this_week_subscriptions),
        this_week_quota_use=_load_quota_use(this_week_quota_use),
    )


if __name__ == "__main__":
    main()
