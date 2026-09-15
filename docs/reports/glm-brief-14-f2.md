# GLM lane report — brief 14, F2: every runtime a KeepAlive agent (2026-09-15)

Lane: DeepSeek-V4.1-Flash via Command Code. Base: `origin/glm/work` at `5aeb72f`.
Branch: `glm/f2-deployments-local-install-py-renders-eve`, one commit, not pushed.

The packet is F2 from `docs/handoff-glm-15.md`.

## What landed

`deployments/local/install.py`:

- **Four runtimes, one template.** `RUNTIMES` names the four local capacity runtimes and
  their defaults — `capacity-loop` (`capacity_loop.py`), `capacity-feed`
  (`capacity_feed.py`), `capacity-web` (`capacity_web.py`), `tick-boards`
  (`tick_boards.py`) — each with a default log beside it under
  `~/.local/share/inference-grid-capacity`. `main` writes one plist per runtime into the
  operator's `--out-dir` and prints the `launchctl bootstrap gui/<uid> <plist>` command for
  each. It still never runs `launchctl`.
- **`KeepAlive`/`RunAtLoad`/`ProcessType: Interactive`, never `StartInterval`.** The one
  template keeps the original shape; the `StartInterval` key is simply absent. The module
  docstring records why: launchd parks interval spawns for a GUI-session agent while the
  display is off ("pended nondemand spawn = interval") — exactly when the phone dashboard is
  the only view — while an already-running process is not held, and a `KeepAlive` agent comes
  back after a reboot. That is the failure the packet is about: the 2026-09-15 reboot killed
  the hand-started feed, dashboard server and boards loop while the one `KeepAlive` scheduler
  survived.
- **The operator's paths reach every plist.** Each runtime takes
  `--<name>-script`/`--<name>-log`; `capacity-loop` also keeps the previous `--script`/`--log`
  spellings, and `--python` is the one interpreter for all four. `render(label, python,
  script, log)` is unchanged for existing callers, so the a2 tests did not move.
- The old single-runtime docstring example, the `LABEL` constant and the `--label` flag are
  gone; the four labels are derived from `DOMAIN` and the runtime name.

## Tests

`tests/test_local_collectors.py` (+2, offline; `plistlib` parses bytes from a temp dir, no
socket):

- `test_main_renders_a_kept_alive_plist_per_runtime_naming_the_operator_paths` — asserts
  `RUNTIMES` is exactly the four names, runs `main` with a distinct script and log per
  runtime, then `plistlib.loads` each plist and checks `Label`, the two-element
  `ProgramArguments` (`[python, script]`), both log paths, `KeepAlive`/`RunAtLoad` true,
  `ProcessType` `Interactive`, and that each bootstrap command was printed.
- `test_no_rendered_plist_carries_a_start_interval` — every rendered plist (raw text and
  parsed dict) has no `StartInterval`.

The two pre-existing `InstallTests` are untouched and pass: `render` still embeds the label,
python, script and log (the log twice), and `main([--out-dir, --python, --script])` still
writes `com.inference-grid.capacity-loop.plist` and prints its bootstrap command.

## Defects found in existing code

None. The packet's premise held: the a2 installer knew only one runtime, and every other
agent an operator hand-started was invisible to it. The two paths that were not obviously a
python file (`capacity-feed`, `capacity-web`) are the operator's; the installer keeps them as
interpreter+script argv like the other two, so the plist shape stays uniform as the packet
asks.

This is a `deployments/local/` file, not one of the provider-authored "integrated unmodified"
modules, so no caller-vs-module split was needed.

## Final gate

- `pytest -q`: **88 failed, 452 passed, 7 skipped** (+2 new tests). The 88 are the
  pre-existing sandbox/`killpg`/`PermissionError` failures — the same set the base reports
  (count unchanged at 88 before and after this packet; only `install.py` and its tests
  changed, and nothing else imports `install`).
- `ruff format` and `ruff check` on `deployments/local/install.py` and
  `tests/test_local_collectors.py`: clean.
- `git grep -n "/Users/" -- deployments/local`: clean (defaults stay `Path.home()`).
- `git log origin/glm/work..HEAD --oneline` names exactly one commit, with the DeepSeek
  trailer.

## Not verified here, and the operator step

- The feed and dashboard-server scripts are the operator's own (`capacity-feed`, and the
  PWA server documented in `docs/CAPACITY-PWA.md`), so the tests pass their paths rather than
  running them. `capacity-web` is modelled as `python <script>`; an operator whose web
  runtime is the `inference-grid-capacity` console script passes that script path and it
  renders the same argv shape.
- Operator step: copy the deployed directory to
  `~/.local/share/inference-grid-capacity/`, run
  `install.py --out-dir ~/Library/LaunchAgents --python <venv python>` (adding the feed, web
  and boards script paths if they are not the defaults), then `launchctl bootstrap` each
  printed command. The previous three timer agents can be retired: these four are kept alive
  instead of scheduled.
