"""Alarm fixtures and edge triggering for the watch code node (handoff brief 14 A1).

Everything is offline: fixture files, a temp sqlite ledger, injected fetch/now/
notify seams. No test opens a socket or runs a subprocess for real.
"""

import io
import json
import os
import subprocess
import sys
import urllib.error

from inference_grid import cli
from inference_grid.ledger import Ledger, attempts as attempts_table, tasks as tasks_table
from inference_grid.watch import git_deploy_commit, watch

NOW = 1_800_000_000.0
RENOTIFY = 400.0


def iso(ts):
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def make_spec(tmp_path, **overrides):
    spec = {"state": str(tmp_path / "watch-state.json"), "thresholds": {"renotify_s": RENOTIFY}}
    spec.update(overrides)
    return spec


def kinds(output):
    return [
        entry["event"] + ":" + entry["kind"] + ":" + str(entry["key"]) for entry in output["delta"]
    ]


def test_reading_stale_names_status_and_age(tmp_path):
    overlay = tmp_path / "overlay.json"
    overlay.write_text(
        json.dumps(
            {
                "accounts": [
                    {"provider": "fresh-ok", "status": "ok", "observed_at": iso(NOW - 60)},
                    {"provider": "stale-ok", "status": "ok", "observed_at": iso(NOW - 3600)},
                    {"provider": "no-observation", "status": "ok", "observed_at": None},
                    {"provider": "claude", "status": "auth_required", "observed_at": iso(NOW - 60)},
                    {"provider": "go", "status": "rate_limited", "observed_at": iso(NOW - 60)},
                    {"provider": "codex", "status": "unknown", "observed_at": iso(NOW - 60)},
                    {"provider": "cline", "status": "error", "observed_at": iso(NOW - 60)},
                ]
            }
        )
    )
    output = watch(make_spec(tmp_path, overlay=str(overlay)), now=NOW)
    raised = {e["key"]: e for e in output["delta"]}
    assert set(raised) == {"stale-ok", "no-observation", "claude", "go", "codex", "cline"}
    assert all(e["event"] == "raised" and e["kind"] == "reading_stale" for e in raised.values())
    assert raised["claude"]["detail"]["status"] == "auth_required"
    assert raised["go"]["detail"]["status"] == "rate_limited"
    assert raised["codex"]["detail"]["status"] == "unknown"
    assert raised["cline"]["detail"]["status"] == "error"
    assert raised["stale-ok"]["detail"] == {"status": "ok", "stale": True}
    assert raised["stale-ok"]["since"] == NOW - 3600
    assert raised["no-observation"]["since"] is None


def test_unreadable_overlay_is_an_error_not_an_alarm(tmp_path):
    overlay = tmp_path / "overlay.json"
    overlay.write_text("{not json")
    output = watch(make_spec(tmp_path, overlay=str(overlay)), now=NOW)
    assert output["delta"] == []
    assert output["errors"] == [{"input": "overlay", "error": "JSONDecodeError"}]


def seed_held(ledger, aid, task, updated, state="held", reason="provider cooldown"):
    with ledger.tx() as con:
        con.execute(tasks_table.insert().values(id=task, project="p", spec={}))
        con.execute(
            attempts_table.insert().values(
                id=aid,
                task=task,
                account="acct",
                generation=1,
                state=state,
                estimate={},
                workspace="/w/" + aid,
                reason=reason,
                updated=updated,
            )
        )


def test_held_alarm_from_a_temp_ledger(tmp_path):
    database = "sqlite:///" + str(tmp_path / "board.sqlite")
    ledger = Ledger(database)
    ledger.initialize()
    seed_held(ledger, "old-held", "task-1", NOW - 2000)
    seed_held(ledger, "new-held", "task-2", NOW - 10)
    seed_held(ledger, "old-done", "task-3", NOW - 2000, state="completed")
    output = watch(make_spec(tmp_path, database=database), now=NOW)
    assert kinds(output) == ["raised:attempt_held:old-held"]
    entry = output["delta"][0]
    assert entry["since"] == NOW - 2000
    assert entry["detail"] == {"task": "task-1", "reason": "provider cooldown"}


def ready_board(tmp_path, name, states):
    board_dir = tmp_path / ("board-" + name)
    board_dir.mkdir(exist_ok=True)
    for i, state in enumerate(states):
        (board_dir / f"task-{i}.json").write_text(json.dumps({"id": f"task-{i}", "state": state}))
    log = tmp_path / ("tick-" + name + ".log")
    log.write_text("")
    return str(board_dir), str(log)


def log_pass(log, board_dir, results, at=NOW):
    with open(log, "a") as handle:
        handle.write(
            json.dumps({"time": iso(at), "board": board_dir, "seconds": 1.0, "results": results})
            + "\n"
        )


def test_board_stalled_needs_ready_tasks_and_consecutive_idle_passes(tmp_path):
    board_dir, log = ready_board(tmp_path, "vix-rs", ["ready", "blocked"])
    spec = make_spec(
        tmp_path,
        boards=[{"name": "vix-rs", "board_dir": board_dir, "tick_log": log}],
    )
    # One idle pass is under the default of two; no alarm yet.
    log_pass(log, board_dir, [{"task": "task-0", "result": "lane_busy"}], at=NOW - 600)
    assert watch(spec, now=NOW)["delta"] == []
    log_pass(log, board_dir, [{"task": "task-0", "result": "no lane available"}], at=NOW - 300)
    output = watch(spec, now=NOW)
    assert kinds(output) == ["raised:board_stalled:vix-rs"]
    assert output["delta"][0]["since"] == NOW - 600
    assert output["delta"][0]["detail"] == {"ready": 1, "passes": 2}
    # A productive result (refused names a real dispatch outcome) clears the alarm.
    log_pass(log, board_dir, [{"task": "task-0", "result": "refused: quota stale"}], at=NOW - 10)
    assert kinds(watch(spec, now=NOW)) == ["cleared:board_stalled:vix-rs"]
    # passed / held / rejected tokens count too: each keeps the board clear afterwards.
    for token in ("passed", "held", "review_rejected"):
        log_pass(log, board_dir, [{"task": "task-0", "result": token}], at=NOW)
        assert watch(spec, now=NOW)["delta"] == []


def test_board_stalled_ignores_boards_without_ready_tasks(tmp_path):
    board_dir, log = ready_board(tmp_path, "quiet", ["blocked", "passed"])
    log_pass(log, board_dir, [{"task": "task-0", "result": "lane_busy"}])
    log_pass(log, board_dir, [])
    spec = make_spec(tmp_path, boards=[{"name": "quiet", "board_dir": board_dir, "tick_log": log}])
    assert watch(spec, now=NOW)["delta"] == []


def test_upload_stale_missing_old_and_not_ok(tmp_path):
    path = tmp_path / "upload-status.json"
    missing, stale, failed, fresh = (
        make_spec(tmp_path, upload_status=str(path), state=str(tmp_path / f"s{n}.json"))
        for n in range(4)
    )
    output = watch(missing, now=NOW)
    assert kinds(output) == ["raised:upload_stale:upload"]
    assert output["delta"][0]["detail"] == {"missing": True}
    path.write_text(json.dumps({"status": "ok", "attempted_at": iso(NOW - 60)}))
    os.utime(path, (NOW - 1000, NOW - 1000))
    output = watch(stale, now=NOW)
    assert output["delta"][0]["since"] == NOW - 1000
    assert output["delta"][0]["detail"]["status"] == "ok"
    path.write_text(json.dumps({"status": "failed"}))
    os.utime(path, (NOW - 1, NOW - 1))
    output = watch(failed, now=NOW)
    assert output["delta"][0]["detail"]["status"] == "failed"
    path.write_text(json.dumps({"status": "ok"}))
    os.utime(path, (NOW - 1, NOW - 1))
    assert watch(fresh, now=NOW)["delta"] == []


def fetch_json(payload):
    return lambda url: ("application/json", json.dumps(payload).encode())


def test_cloud_behind_compares_commits(tmp_path):
    spec = make_spec(
        tmp_path,
        cloud_health_url="https://host/healthz",
        deploy_dir=str(tmp_path / "deployments" / "capacity"),
    )
    output = watch(
        spec,
        now=NOW,
        fetch=fetch_json({"ok": True, "commit": "cloud-1"}),
        deploy_commit=lambda deploy_dir: "deploy-1",
    )
    assert kinds(output) == ["raised:cloud_behind:cloud"]
    assert output["delta"][0]["detail"] == {
        "cloud_commit": "cloud-1",
        "deploy_commit": "deploy-1",
    }
    assert kinds(
        watch(
            spec,
            now=NOW,
            fetch=fetch_json({"ok": True, "commit": "deploy-1"}),
            deploy_commit=lambda _: "deploy-1",
        )
    ) == ["cleared:cloud_behind:cloud"]


def test_unversioned_health_body_is_reported_once_per_kind(tmp_path):
    for n, (fetch, detail) in enumerate(
        (
            (lambda url: ("text/plain", b"ok"), {"content_type": "text/plain"}),
            (fetch_json({"ok": True, "commit": None}), {"content_type": "application/json"}),
            (
                lambda url: (_ for _ in ()).throw(urllib.error.URLError("down")),
                {"error": "URLError"},
            ),
        )
    ):
        spec = make_spec(
            tmp_path,
            cloud_health_url="https://host/healthz",
            deploy_dir=str(tmp_path / "deployments" / "capacity"),
            state=str(tmp_path / f"s{n}.json"),
        )
        output = watch(spec, now=NOW, fetch=fetch, deploy_commit=lambda _: "deploy-1")
        assert kinds(output) == ["raised:cloud_health_unversioned:cloud"]
        assert output["delta"][0]["detail"] == detail


def test_git_deploy_commit_uses_the_packet_form(monkeypatch, tmp_path):
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout=b"abc123\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    deploy = tmp_path / "deployments" / "capacity"
    assert git_deploy_commit(deploy) == "abc123"
    assert seen["argv"] == [
        "git",
        "-C",
        str(deploy),
        "log",
        "-1",
        "--format=%H",
        "--",
        ".",
    ]


def run_cli(monkeypatch, tmp_path, command, payload, database="sqlite:///" + "unused.sqlite"):
    argfile = tmp_path / (command + ".json")
    argfile.write_text(json.dumps(payload))
    monkeypatch.setattr(
        sys, "argv", ["inference-grid", "--database", database, command, "--json", str(argfile)]
    )
    buffer = io.StringIO()
    real = sys.stdout
    sys.stdout = buffer
    try:
        cli.main()
    finally:
        sys.stdout = real
    return buffer.getvalue()


def test_edge_triggering_against_the_state_file(tmp_path):
    overlay = tmp_path / "overlay.json"
    overlay.write_text(
        json.dumps(
            {"accounts": [{"provider": "claude", "status": "error", "observed_at": iso(NOW - 60)}]}
        )
    )
    spec = make_spec(tmp_path, overlay=str(overlay))
    clock = {"t": NOW}
    output = watch(spec, now=lambda: clock["t"])
    assert kinds(output) == ["raised:reading_stale:claude"]
    assert output["delta"][0]["since"] == NOW - 60
    assert (
        json.loads((tmp_path / "watch-state.json").read_text())["alarms"][0]["notified_at"] == NOW
    )
    # Unchanged: silent, state untouched.
    clock["t"] = NOW + 60
    assert watch(spec, now=lambda: clock["t"])["delta"] == []
    # Cleared when the overlay recovers.
    overlay.write_text(
        json.dumps(
            {"accounts": [{"provider": "claude", "status": "ok", "observed_at": iso(NOW + 60)}]}
        )
    )
    clock["t"] = NOW + 120
    output = watch(spec, now=lambda: clock["t"])
    assert kinds(output) == ["cleared:reading_stale:claude"]
    assert json.loads((tmp_path / "watch-state.json").read_text())["alarms"] == []
    # Raised again, then re-raised once the renotify interval elapses, with the
    # original since preserved so the notifier names the whole duration.
    overlay.write_text(
        json.dumps(
            {
                "accounts": [
                    {"provider": "claude", "status": "rate_limited", "observed_at": iso(NOW + 120)}
                ]
            }
        )
    )
    clock["t"] = NOW + 180
    assert kinds(watch(spec, now=lambda: clock["t"])) == ["raised:reading_stale:claude"]
    clock["t"] = NOW + 180 + RENOTIFY - 1
    assert watch(spec, now=lambda: clock["t"])["delta"] == []
    clock["t"] = NOW + 180 + RENOTIFY
    output = watch(spec, now=lambda: clock["t"])
    assert kinds(output) == ["renotified:reading_stale:claude"]
    assert output["delta"][0]["since"] == NOW + 120
    assert output["delta"][0]["event"] == "renotified"


def test_notify_hook_receives_exactly_the_printed_output(tmp_path, monkeypatch):
    overlay = tmp_path / "overlay.json"
    overlay.write_text(
        json.dumps(
            {"accounts": [{"provider": "claude", "status": "error", "observed_at": iso(NOW)}]}
        )
    )
    script = tmp_path / "notify.sh"
    received = []

    def fake_notify(path, message):
        received.append((path, message))
        return None

    output = watch(
        make_spec(tmp_path, overlay=str(overlay), notify=[str(script)]),
        now=NOW,
        notify=fake_notify,
    )
    assert received == [(str(script), json.dumps(output, indent=2))]
    # The CLI roundtrip: the notifier (patched here) gets the exact text printed.
    messages = []

    def capture(path, message):
        messages.append(message)
        return None

    monkeypatch.setattr("inference_grid.watch.run_notify", capture)
    printed = run_cli(
        monkeypatch,
        tmp_path,
        "watch",
        {
            "overlay": str(overlay),
            "state": str(tmp_path / "cli-state2.json"),
            "notify": [str(script)],
            "thresholds": {"renotify_s": 0},
        },
    )
    assert messages[0] + "\n" == printed


def test_default_notify_runs_the_script_with_the_message_on_stdin(monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["input"] = kwargs.get("input")
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    from inference_grid.watch import run_notify

    assert run_notify("/path/notify.sh", "hello") is None
    assert seen["argv"] == ["/path/notify.sh"]
    assert seen["input"] == b"hello"


def test_notify_failure_is_recorded_and_does_not_stop_the_rest(tmp_path):
    overlay = tmp_path / "overlay.json"
    overlay.write_text(
        json.dumps(
            {"accounts": [{"provider": "claude", "status": "error", "observed_at": iso(NOW)}]}
        )
    )
    broken = tmp_path / "broken.sh"
    good = tmp_path / "good.sh"
    calls = []

    def fake_notify(path, message):
        calls.append(path)
        return "PermissionError" if path == str(broken) else None

    output = watch(
        make_spec(tmp_path, overlay=str(overlay), notify=[str(broken), str(good)]),
        now=NOW,
        notify=fake_notify,
    )
    assert calls == [str(broken), str(good)]
    assert output["errors"] == [
        {"input": "notify", "path": str(broken), "error": "PermissionError"}
    ]


def test_unrelated_passes_do_not_touch_each_other(tmp_path):
    overlay = tmp_path / "overlay.json"
    overlay.write_text(
        json.dumps({"accounts": [{"provider": "claude", "status": "ok", "observed_at": iso(NOW)}]})
    )
    path = tmp_path / "upload-status.json"
    path.write_text(json.dumps({"status": "failed"}))
    spec = make_spec(tmp_path, overlay=str(overlay), upload_status=str(path))
    output = watch(spec, now=NOW)
    assert kinds(output) == ["raised:upload_stale:upload"]
    # The clear of upload does not re-raise or duplicate reading's silence.
    path.write_text(json.dumps({"status": "ok"}))
    os.utime(path, (NOW - 1, NOW - 1))
    assert kinds(watch(spec, now=NOW + 1)) == ["cleared:upload_stale:upload"]
    assert watch(spec, now=NOW + 2)["delta"] == []
