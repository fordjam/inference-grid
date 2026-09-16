"""`inference-grid digest`: the one page the operator reads each morning.

Per board: accepted / passed / blocked / ready counts with the oldest ready task's age;
per lane: attempts, holds and refusals by reason, quota used; the retries `--suggest`
would author; and the inbox landings still awaiting `inbox-integrate`. Markdown to
stdout. Read-only over the boards and the ledger.
"""

import time
from pathlib import Path


CATEGORY_QUALIFICATION = {}


def _inbox_awaiting(project_root, board_rows):
    """Accepted tasks whose landing has no integration checklist on the inbox branch."""
    import subprocess

    awaiting = []
    for row in board_rows:
        if row["id"] and row.get("state") == "accepted":
            record = "grid/inbox/" + row["id"] + ".json"
            checklist = "grid/inbox/" + row["id"] + ".integrate.md"
            landing = subprocess.run(
                ["git", "-C", str(project_root), "show", "grid/inbox:" + record],
                capture_output=True,
            )
            integrated = subprocess.run(
                ["git", "-C", str(project_root), "show", "grid/inbox:" + checklist],
                capture_output=True,
            )
            if landing.returncode == 0 and integrated.returncode != 0:
                awaiting.append(row["id"])
    return awaiting


def digest(ledger, boards_dir, now=None):
    """The one-page markdown digest over every board config in boards_dir."""
    now = time.time() if now is None else now
    from .board.policy import owner_only_prefix
    from .cli import board_status
    from .tick_all import ordered_configs

    lines = [
        "# Operator digest",
        "",
        f"Generated {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(now))}.",
        "",
    ]
    suggestions = []
    inbox = []
    owner_only_rows = []
    seen = set()
    configs = ordered_configs(boards_dir)
    for config in configs:
        name = Path(config["project_root"]).name
        # One entry per board name: a config directory holding duplicates (two files for
        # one board) would otherwise list the board twice with the same counts.
        if name in seen:
            continue
        seen.add(name)
        rows = board_status(ledger, config["board_dir"])
        counts = {}
        oldest_ready = None
        prefixes = config.get("owner_only")
        for row in rows:
            counts[row["state"]] = counts.get(row["state"], 0) + 1
        for path in sorted(Path(config["board_dir"]).glob("*.json")):
            import json as jsonlib

            # The plan node's drafts sidecar (and any other board-owned state) is not a
            # task file; an unreadable one is skipped, never fatal to the digest.
            try:
                task = jsonlib.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if not isinstance(task, dict) or task.get("state") != "ready":
                continue
            if prefixes and task.get("category") == "packet":
                # The autonomy policy's dispatch-time wait (brief 14 M3): a packet the
                # runner would refuse is listed before the operator wonders why it
                # never moves, with the prefix that matched.
                prefix = owner_only_prefix(task, prefixes)
                if prefix is not None:
                    owner_only_rows.append((name, task.get("id") or path.stem, prefix))
            age = (now - path.stat().st_mtime) / 86400
            if oldest_ready is None or age > oldest_ready[0]:
                oldest_ready = (age, task.get("id") or path.stem)
        lines.append(f"## Board {name}")
        lines.append("")
        lines.append(
            "accepted {accepted}, passed {passed}, blocked {blocked}, ready {ready}; "
            "oldest ready task: {oldest}".format(
                accepted=counts.get("accepted", 0),
                passed=counts.get("passed", 0),
                blocked=counts.get("blocked", 0),
                ready=counts.get("ready", 0),
                oldest=(f"{oldest_ready[1]} ({oldest_ready[0]:.1f} d)" if oldest_ready else "none"),
            )
        )
        suggest_report = board_status(ledger, config["board_dir"], suggest=True)
        for row in suggest_report["rows"]:
            if row.get("suggest"):
                suggestions.append((name, row["id"], row["suggest"]["change"]))
        for row in suggest_report["skipped"]:
            suggestions.append((name, row["task"], f"skipped: successor {row['successor']} exists"))
        awaiting = _inbox_awaiting(Path(config["project_root"]), rows)
        if awaiting:
            inbox.append((name, awaiting))
        lines.append("")
    lines += ["## Suggested retries", ""]
    if suggestions:
        for name, tid, change in suggestions:
            lines.append(f"- {name}: `{tid}` — {change}")
    else:
        lines.append("- none")
    lines += ["", "## Inbox landings awaiting inbox-integrate", ""]
    if inbox:
        for name, task_ids in inbox:
            lines.append(f"- {name}: {', '.join('`' + t + '`' for t in task_ids)}")
    else:
        lines.append("- none")
    lines += ["", "## Lane activity", ""]
    lines.append("| lane account | attempts | held | refused |")
    lines.append("| --- | --- | --- | --- |")
    status = ledger.status()
    by_account = {}
    for row in status:
        entry = by_account.setdefault(row["account"], {"attempts": 0, "held": 0, "refused": 0})
        entry["attempts"] += 1
        entry["held"] += row["state"] == "held"
        entry["refused"] += bool(row.get("reason") and str(row["reason"]).startswith("Refused"))
    for account in sorted(by_account):
        entry = by_account[account]
        lines.append(f"| {account} | {entry['attempts']} | {entry['held']} | {entry['refused']} |")
    if not by_account:
        lines.append("| (no attempts) | 0 | 0 | 0 |")
    from .board.evals import EVAL_EVERY_DAYS, eval_markdown, eval_summary, lanes_from_configs

    lanes = lanes_from_configs(configs)
    summary = eval_summary(ledger, lanes, now=now) if lanes else None
    lines += ["", "## Evals", ""]
    if summary is None:
        lines.append("- no lanes configured (a board config names the lanes file)")
    else:
        lines += eval_markdown(summary)
    # O1: what is cheap this week on the plans, and what using it would take.
    deal_rows = []
    try:
        from .catalogue import deals, deals_lines

        catalogue = ledger.catalogue()
        if catalogue:
            deal_rows = deals(catalogue, lanes or {}, ending_within_days=3)
    except Exception:  # noqa: BLE001 — a legacy ledger without the table has no deals to list
        deal_rows = []
    lines += ["", "## Deals", ""]
    if deal_rows:
        lines += [f"- {line}" for line in deals_lines(deal_rows)]
    else:
        lines.append("- none recorded (run deployments/local/collect_catalogue.py)")
    lines += ["", "## Needs you", ""]
    for d in deal_rows:
        if d["ending_soon"] and d["state"] != "qualified":
            lines.append(
                f"- deal ending: `{d['provider']}/{d['model']}` {d['deal']} ends in "
                f"{d['days_left']}d and is {d['state']}"
            )
    for board_name, task_id, prefix in owner_only_rows:
        lines.append(
            f"- owner-only: `{task_id}` — a declared file is under prefix `{prefix}`"
            f" (board {board_name})"
        )
    if summary is not None and summary["needs_you"]:
        for row in summary["needs_you"]:
            lines.append(f"- eval coverage: `{row['lane']}` — {row['reason']}")
    elif not owner_only_rows:
        if summary is not None:
            lines.append(f"- none; every lane has an eval result inside {EVAL_EVERY_DAYS} days")
        else:
            lines.append("- none")
    return "\n".join(lines) + "\n"
