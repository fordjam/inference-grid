# Release check — 0.1.0a5 (2026-09-13)

Everything the README quickstart claims, from the wheel alone in a clean temp venv.
Recorded verbatim from the run on this machine (macOS, Python 3.12, pip 26.1.2).

## 1. Build the wheel

```
$ ~/.local/share/inference-grid/venv/bin/python -m build --wheel --no-isolation --outdir dist .
Successfully built inference_grid-0.1.0a5-py3-none-any.whl
```

`--no-isolation` keeps the build offline: the venv's own setuptools (84.0.0) and build
(1.6.1) are used instead of downloading an isolated build environment. Both are in the
`test` extra (`pip install -e '.[test]'`), added this release.

## 2. Install into a clean temp venv

```
$ python3.12 -m venv /tmp/ig-relcheck
$ /tmp/ig-relcheck/bin/pip install dist/inference_grid-0.1.0a5-py3-none-any.whl
$ /tmp/ig-relcheck/bin/pip list | grep -iE "inference|sqlalchemy|celery|psycopg"
celery            5.6.3
inference-grid    0.1.0a5
psycopg           3.3.5
psycopg-binary    3.3.5
SQLAlchemy        2.0.52
```

The wheel plus its declared dependencies install cleanly; the runtime dependencies
(sqlalchemy, psycopg, celery) resolve from PyPI at install time.

## 3. Entry points

```
$ /tmp/ig-relcheck/bin/inference-grid --help
usage: inference-grid [-h] [--database DATABASE] {init,doctor,defer,cooldown,account,submit,claim,status,run,publish,hold-abandoned,accept,resolve,refresh-collect,lane,outcome,scorecard,board-tick,board-new,board-status,inbox-integrate,evaluation,tick} ...
$ /tmp/ig-relcheck/bin/inference-grid-lane --help
usage: inference-grid-lane [-h] --config CONFIG lane
```

(`tests/test_packaging.py` asserts the same from the wheel: every `src/inference_grid`
module present and the console entry points declared, including
`inference-grid-lane = inference_grid.lanes.runner:main`.)

## 4. `inference-grid doctor` against a temp ledger

```
$ /tmp/ig-relcheck/bin/inference-grid init --database sqlite:////tmp/ig-relcheck/ledger.sqlite
$ /tmp/ig-relcheck/bin/inference-grid account --database sqlite:////tmp/ig-relcheck/ledger.sqlite --json account.json
$ /tmp/ig-relcheck/bin/inference-grid lane --database sqlite:////tmp/ig-relcheck/ledger.sqlite --json lane.json
$ /tmp/ig-relcheck/bin/inference-grid doctor --database sqlite:////tmp/ig-relcheck/ledger.sqlite
{
  "scope": "ledger_and_adapter_executables",
  "status": "checks_passed",
  "database": "readable",
  "provider_auth": "not_checked",
  "broker": "not_checked",
  "findings": [],
  "counts": { "accounts": 1, "collecting": 0, "stuck_collections": 0,
    "inference_cooldowns": 0, "usage_cooldowns": 0, "stale_accounts": 0,
    "queued": 0, "dispatching": 0, "held": 0, "resolved": 0,
    "pending_publications": 0, "missing_executables": 0,
    "unresolved_executables": 0, "invalid_adapter_specs": 0 },
  "lanes": [ { "provider": "go", "state": "ready", "reason": "ready",
    "next_check_at": 1789339499 } ]
}
```
Exit code 0.

## 5. `inference-grid board-tick --dry-run` against a temp board

Temp board: one `ready` `pure_function` task (`copy-mod`), a private `lanes.json` with one
`go_http` lane, and `tick.json`:

```
{"board_dir": "/tmp/ig-relboard/grid/board", "project_root": "/tmp/ig-relboard",
 "lanes_path": "/tmp/ig-relboard/lanes.json",
 "accounts_by_lane": {"go": "go-check-alias"},
 "packets_root": "/tmp/ig-relboard/packets", "dry_run": true}
```

```
$ /tmp/ig-relcheck/bin/inference-grid board-tick --database sqlite:////tmp/ig-relcheck/ledger.sqlite --json tick.json
{
  "readiness": { "go": { "state": "ready" } },
  "plan": [ { "task": "copy-mod", "lane": "go", "reason": "selected" } ]
}
```
Exit code 0; no attempt created, no task file written, no packet directory touched.

## 6. Packaging test in CI

`tests/test_packaging.py` rebuilds the wheel with `--no-isolation` on every test run and
asserts the wheel carries every `src/inference_grid` module, the console entry points
(including `inference-grid-lane`), and the `capacity_web` assets.
