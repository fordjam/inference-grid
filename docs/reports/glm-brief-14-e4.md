# GLM lane report — brief 14, E4: streaming transcript compaction and a disk guard (2026-09-15)

Lane: DeepSeek-V4.1-Flash via Command Code. Base: `origin/glm/work` at `4878554`.
Branch: `glm/e4-streaming-transcript-compaction-and-a-di`, one commit, not pushed.

The packet is E4 from `docs/handoff-glm-15.md`; the driver names this row and report
"brief 14", so they follow the driver's naming.

## What landed

`src/inference_grid/lanes/packet.py`:

- **`stream_transcript(stream, native)`** (new) — the line filter. Copy the agent's stdout
  into `native`, dropping `DELTA_EVENTS` (`thinking_delta`, `text_delta`, `content_delta`)
  as they arrive, and write the running count beside the transcript in
  `native-<n>.compacted.json` as `{"dropped_delta_events": n}` — the same file name and
  shape `compact_transcripts` already writes. The delta test is factored into
  **`_is_delta_line(raw)`**, shared by both the new filter and the unchanged
  `compact_transcripts`, so the two cannot drift.
- **`build_loop` streams instead of redirecting.** The round's stdout was
  `stdout=<file>`; it is now `stdout=subprocess.PIPE` and a daemon thread runs
  `stream_transcript(proc.stdout, native)` while the main thread enforces the wall
  deadline on `proc.wait(timeout=…)`. The reader is joined (30 s cap) before the session id
  is read from the transcript, so `native-<n>.jsonl` is complete — and delta-free — by the
  time anything consumes it. On a wall-deadline kill the process group dies first, the pipe
  closes, and the reader ends.
- **The disk guard.** Before every round the loop measures free space under the packets root
  (`shutil.disk_usage(attempt_dir)`, and `attempt_dir` lives under the packets root, so it
  is that filesystem) and refuses to start when it is below `min_free_bytes` — default
  `DEFAULT_MIN_FREE_BYTES = 5 * 1024**3`, the packet's 5 GB. That verdict is
  `reason: disk_low`, with **no round started** (no argv built, no process, no
  `native-<n>.jsonl`). The measurement is injectable as `free_bytes=free_disk_bytes`, a
  path-taking callable, mirroring the existing `clock=time` seam.
- **The verdict carries the numbers.** `min_free_bytes` and `disk_free_bytes` (the last
  figure the guard measured, or the figure at refusal) are in `verdict.json`, so a full disk
  is named rather than showing up as an unexplained stop.
- `compact_transcripts` and its behaviour are untouched: `scripts/run_lane.py` still calls
  it after the loop, which is what covers attempts written before this change and resumed
  attempts whose older `native-*.jsonl` files predate the streaming path.

## Tests

`tests/test_packet_lane.py` (+3, all offline, real subprocesses through the `plain`
sandbox):

- `test_streaming_transcript_leaves_the_file_free_of_delta_events` — `StreamingAgent`
  (new fake CLI) writes one `run_start`, 10 000 `thinking_delta` lines and two
  `tool_*` events. `native-1.jsonl` holds exactly the 3 non-delta lines and no `delta`
  substring; `native-1.compacted.json` reads `{"dropped_delta_events": 10000}`.
- `test_the_disk_guard_refuses_a_round_below_the_threshold` — with
  `free_bytes` injected one byte below `min_free_bytes`, the verdict is `disk_low`,
  `rounds == []`, the agent was never invoked, `disk_free_bytes`/`min_free_bytes` are
  recorded, and no `native-1.jsonl` exists.
- `test_the_verdict_records_the_free_space_the_guard_measured` — with a comfortable
  injected figure the loop runs and the verdict records it.

No existing test changed: the loop's other tests (the early-stop suite, `test_lane_brief`'s
`compact_transcripts` check, the board's `test_packet_task.py`) pass with the transcript
now routed through the filter, since their fake CLIs emit no delta lines. The default
5 GB guard does not trip on this machine (the workspace volume reports ~11 GiB free).

## Defects found in existing code

None. The packet's premise held: `build_loop` did redirect stdout straight to the file, and
`compact_transcripts` was the only compaction, applied after the attempt.

## Final gate

- `pytest -q`: **88 failed, 432 passed, 7 skipped** (+3 new tests). The baseline-aware gate
  was run directly (`python -m inference_grid.lanes.gates pytest origin/glm/work <cache>`):
  **88 inherited, 0 repaired, no new failures, exit 0**. The 88 are the pre-existing
  sandbox/`killpg`/`PermissionError` failures, identical between base and branch.
- `ruff format` and `ruff check` on `src/inference_grid/lanes/packet.py` and
  `tests/test_packet_lane.py`: clean.
- `git log origin/glm/work..HEAD --oneline` names exactly one commit, with the DeepSeek
  trailer.

## Not verified here, and one residual risk

- Real end-to-end runs of a CLI that streams deltas (Command Code, Cline) were not possible
  in this lane, so the streaming path is exercised through real subprocesses in the tests
  rather than the production adapters. A CLI that writes its NDJSON without line breaks
  would present a single huge line to the filter, which would be kept whole; every adapter
  here is line-delimited (`jsonl`), so this is the documented assumption.
- If a killed agent leaves a detached grandchild holding the stdout pipe, `reader.join`
  caps at 30 s and the loop proceeds; the daemon reader would finish its copy whenever the
  pipe closes. The production kill path signals the whole process group, so this is a
  last-resort bound, not the normal path.
