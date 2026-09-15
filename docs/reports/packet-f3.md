# Report — packet F3, a job file for the driver (GLM-5.3-Flash)

Lane: GLM-5.3-Flash via Command Code. Branch `packet/packet-f3` from `glm/work`
at `0f29db0`. One commit, not pushed; `glm/work` and `main` untouched.

The packet is F3 from `docs/handoff-glm-15.md` (Phase F): `scripts/run_lane.py --job
lanes.json` replaces the per-lane flag soup, the existing flags keep working for one lane,
and every lane entry runs as its own subprocess with a `lane-<name>.log` beside the packets.

## What landed

- **`src/inference_grid/lanes/job.py`** (new) — the job document as a tested module, so the
  driver's parent mode and, later, the board read one definition:
  - `load_job(path)` reads the JSON and validates it: unknown **top-level** keys
    (`repo`, `brief`, `base`, `python`, `packets_root`, `database`, `lanes`) and unknown
    **lane** keys (`name`, `clone`, `adapter`, `model`, `packets`, `key_file`) are refused by
    name; missing required keys are named; `lanes` must be a non-empty list; lane names must
    be simple identifiers and unique (they name the log files); the adapter is one of
    `command_code` / `cline`, and a `cline` entry must carry `key_file`. **Relative paths
    resolve against the file's own directory** — `repo`, `brief`, `python`, `packets_root`,
    `clone`, `key_file` — while `~` expands to the operator's home and `database` stays a URL.
    Failures raise `JobError(ValueError)` whose message names the key.
  - `lane_argv(job, lane, script)` renders exactly the flags the one-lane driver still takes
    (`--repo --clone --brief --packets … --base --lane --model --adapter --python
    --packets-root`, plus `--database` and `--cline-key-file` when the job names them).
  - `lane_log(job, lane)` is `<packets_root>/lane-<name>.log`.
  - `run_job(job_path, spawn=…)` creates the packets root, launches **one subprocess per
    entry** through the injected `spawn(argv, log_path) -> returncode` (a `ThreadPoolExecutor`
    runs them concurrently; the default spawn runs the real driver with both streams on the
    lane's log), collects every result — a lane that raises is reported as `error: …` without
    hiding the others — prints a fixed-width table (`lane`, `packets`, `result`) and returns 0
    only when every lane passed.
- **`scripts/run_lane.py`** — gains `--job`; `--repo`, `--clone`, `--brief`, `--packets` and
  `--python` are now required *only* without it, and mixing `--job` with any of them is an
  argparse error. Job mode delegates to `job.run_job` and a `JobError` exits 2 through
  `p.error`. The single-lane path is unchanged, so one lane still runs from flags alone.
- **`tests/test_lane_job.py`** (7 offline cases, injected spawn — no lane, no socket).
- **`docs/ARCHITECTURE.md`** — the "Run build packets through a lane" section shows the job
  file and `--job`, with the flags kept as the one-lane form.

## The job file

```json
{"repo": ".", "brief": "docs/handoff-glm-15.md", "base": "glm/work",
 "python": ".venv/bin/python", "packets_root": "~/.grid-workspaces/packets",
 "database": "sqlite:///...",
 "lanes": [{"name": "goat-glm", "clone": "~/.grid-workspaces/ig-lane-a",
            "adapter": "command_code", "model": "z-ai/glm-5.3-flash", "packets": ["E1", "E2"]},
           {"name": "cline-glm", "clone": "~/.grid-workspaces/ig-lane-c",
            "adapter": "cline", "model": "z-ai/glm-5.3-flash",
            "key_file": "~/.config/inference-grid/cline-pass.key", "packets": ["F1"]}]}
```

## Tests

`tests/test_lane_job.py`, all offline:

- relative `repo`/`brief`/`python`/`packets_root`/`clone`/`key_file` resolve against the job
  file's directory, `clone: "~/lane-c"` expands to the home directory, and adapter/model
  defaults apply.
- unknown top-level key, unknown lane key, missing top-level key, empty `lanes`, duplicate
  lane name, unknown adapter, `cline` without `key_file`, empty `packets` and non-JSON are
  each refused with the key named.
- `lane_argv` carries every single-lane flag, the packet ids in order, `--database` when set,
  and `--cline-key-file` only for the `cline` lane.
- `run_job` with an injected spawn calls it once per entry, with the argv's `--lane` naming
  each lane and the log at `<packets_root>/lane-<name>.log`; the packets root is created.
- a lane whose spawn returns non-zero prints `failed (3)` and makes `run_job` return 1; a
  lane whose spawn raises prints `error: …` and does not hide the other lane.
- `main([])` exits without a job, `main(["--job", …, "--clone", x])` refuses the mix, and
  `main(["--job", …])` delegates to `run_job`.

## Verification

- `ruff format --check` and `ruff check` clean on `scripts/run_lane.py`,
  `src/inference_grid/lanes/job.py` and `tests/test_lane_job.py`. (The venv's ruff still
  format-disagrees with pre-existing files under `src/inference_grid/lanes` — six of them,
  provider-authored — which were not touched.)
- Full suite (`PYTHONPATH=src … -m pytest -q -p no:cacheprovider
  --continue-on-collection-errors`): **88 failed, 488 passed, 7 skipped, 1 error**. The
  failing node-id set was diffed line-by-line against a scratch worktree at `HEAD`
  (`0f29db0`) — **byte-identical**, 88/88 (**88 inherited, 0 new**); the +7 passes are
  exactly the new tests.
- The one collection error is `tests/test_calibration.py`: it `stat`s a path under the
  credential directory at import time, which this sandbox denies (`PermissionError`). It is
  present at `HEAD` too and is inherited — the module-level skip condition is evaluated
  before pytest can skip it. Nothing in the packet touches it.

## Defects found in existing code, not fixed

- `scripts/run_lane.py` hard-codes the brief number: attempt directories are
  `lane-<lane>-14-<packet>`, the summary is `lane-<lane>-14-<stamp>.summary.json` and the
  prompt's report name is `glm-brief-14-<packet>.md`. A job pointing at brief 15 still tells
  the agent to write `glm-brief-14-*.md`. Out of scope for a job-file packet; the fix is to
  derive the number from the brief's own heading.
- D1's board integration (`board/packet_task.py`) does **not** read this file: it carries its
  own `spec {brief, packet_id, gates, base}` on the board task. The packet's line "the file
  D1's board integration reads too" is therefore aspirational as landed — `lanes/job.py` is
  importable for it, but wiring it in (a `job` path on the packet spec, or a lane list) is a
  separate change.
- In job mode an unrelated flag that has a non-`None` default (e.g. `--model`, `--branch-prefix`)
  is silently ignored rather than refused; only the five required single-lane flags are
  checked for the `--job` mix. The job file's per-lane `model` is authoritative, which is the
  intent, but a caller passing both gets no warning.

## Not verified here / operator step

- No real lane was launched and no job file was written outside the tests: the path is
  exercised only through the injected spawn (the hard rules forbid the live `board-tick`
  and any subscription spend, and `~/.grid-workspaces` is look-only). The first live use is
  the operator's next multi-lane round; `docs/ARCHITECTURE.md` shows the file it takes.
