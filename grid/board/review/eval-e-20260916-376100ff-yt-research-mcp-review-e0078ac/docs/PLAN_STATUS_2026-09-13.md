# Plan status — 2026-09-13

One row per item in `docs/PLAN.md` (the amended, superseding plan) and the
`docs/ORIGINAL_PLAN.md` items it carries forward. Statuses: **implemented**
(module plus a test in this repository that proves it), **partial** (what is
missing), **absent**. Evidence is code and tests in this repository only;
nothing under `strategy-lab/` was read for this review (existence checks
only), and the research-library package was inventoried but has no plan rows
to check against (see "Beyond the plan").

## 1. Layout (PLAN §1)

| item | status | evidence |
| --- | --- | --- |
| uv project; scripts `yt-research`, `yt-research-mcp` | implemented | `pyproject.toml [project.scripts]` |
| `src/yt_research/{ids,metadata,discovery,captions,asr,transcripts,cache,errors,ytdlp}.py` | implemented | all nine modules present |
| `mcp_server.py`, `cli.py` in the package, thin wrappers | implemented | `src/yt_research/mcp_server.py`, `cli.py`; no MCP/CLI imports in the library (package docstring states it; import graph confirms) |
| `__init__` re-exports the public API | implemented | `__init__.py` `__all__`: the 7 functions + `ResearchError`, `ERROR_CODES`, `__version__` |
| committed fixtures (json3, VTT, flat extraction, info dict) | implemented | `tests/fixtures/{manual,auto_wordlevel}.json3`, `auto_rolling.vtt`, `flat_entries.json`, `info_dict.json` |
| `.claude/skills/` research + strategy-spec skills | implemented (existence) | `.claude/skills/{yt-research,strategy-spec,library-triage}/` — contents not reviewed under this brief |
| `strategy-lab/` templates + rubric | implemented (existence) | `strategy-lab/templates/strategy-spec.md`, `strategy-lab/rubric/flaws.md` — contents not read (brief forbids strategy-lab reads) |
| extra `logging_setup.py` module | beyond plan | present; harmless addition (stderr logging setup) |

## 2. Public Python API (PLAN §2)

| item | status | evidence |
| --- | --- | --- |
| `search_videos(query, *, max_results, channel, sort, upload_after, kind)` — exact signature | implemented | `discovery.py:125`; `tests/test_discovery.py` (19 tests) |
| channel restriction via the `/search?query=` tab, falling back to listing + title filter | implemented | `discovery.py::channel_search_url` (`ids.py:196`) + `_search_in_channel` (`discovery.py:197`); covered in `tests/test_discovery.py` |
| `upload_after` client-side filter; entries without a date kept, limitation documented | implemented | `discovery.py::_filter_upload_after` + docstring ("Filtered client-side and only …") |
| `list_channel_videos(channel, *, max_results=50, tab)` — handle/URL/ID forms | implemented | `discovery.py:224`; `ids.py` channel forms tested in `tests/test_ids.py` (10 tests) |
| `list_playlist(playlist, *, max_results=200)` | implemented | `discovery.py:262` |
| `get_metadata(url)` + `is_live`, `was_live`, `availability`, `tags`, `categories`, `chapters` | **partial** | mapping implemented (`metadata.py:47-53`); live behaviour covered by `tests/integration/test_live.py`, CLI error path by `tests/test_cli.py:149` — but **no offline unit test asserts the extended-field mapping against the committed `info_dict.json` fixture** (packet B1) |
| `fetch_transcript(url, *, lang, mode, refresh, start, end)`; `window` + joined `text` in output | implemented | `transcripts.py:100`, `_window`/`_finalize` (`transcripts.py:48-62`); `tests/test_transcripts.py` (18 tests) |
| segment normalisation: rolling-caption de-dup, merge, noise tags | implemented | `captions.py::dedupe_cues:391`, pipeline `captions.py:486`; `tests/test_captions.py` (39 tests, incl. `auto_rolling.vtt` fixture) |
| `list_cached()` | implemented | `cache.py:252`; `tests/test_cache.py` (25 tests) |
| `search_cached(query, *, limit, video_id)` — FTS5, `{video_id, title, start, end, text, snippet}` | implemented | `cache.py:297`, FTS5 table `cache.py:43`, `snippet()` at `cache.py:325` |
| payload fields `caption_kind`, `retrieved_at`, `cached`, `source` | implemented | `transcripts.py:170-174,218` |

## 3. MCP server (PLAN §3)

| item | status | evidence |
| --- | --- | --- |
| FastMCP stdio server wrapping the API | implemented | `mcp_server.py` (imports `FastMCP`, with an `mcp >= 2.0` `MCPServer` alias fallback — beyond-plan resilience) |
| one tool per API function; errors returned as data, never raised | implemented | `mcp_server.py:50-56` (`{"error": {code, message, details}}`); `tests/test_mcp_server.py` (10 tests), protocol coverage in `tests/test_mcp_protocol.py` |
| logging to stderr, level from `YT_MCP_LOG_LEVEL` | implemented | `logging_setup.py`; README documents it |
| "7 tools" | implemented — and superseded | the plan's 7 research tools are all advertised; the server now exposes 20 (README line 20) because the beyond-plan research-library tools were added. Plan growth, not a gap. |

## 4. CLI (PLAN §4)

| item | status | evidence |
| --- | --- | --- |
| subcommands mirroring the API; `--json` prints the exact API dict | implemented | `cli.py:120-159` (`search`, `channel`, `playlist`, `metadata`, `transcript`, `cached`, `search-cached`); `tests/test_cli.py` (15 tests) |
| `transcript --txt` prints `[mm:ss] text` lines | implemented | `cli.py:154`; tested in `tests/test_cli.py` |
| `--version` | implemented | `cli.py:120` |

## 5. Build phases (PLAN §5 acceptance checks)

| phase | status | evidence |
| --- | --- | --- |
| 0 scaffold: server advertises tools; CLI `--help` | implemented | `tests/test_mcp_server.py`, `tests/test_cli.py` |
| 1 IDs + metadata; deleted video → `video_unavailable` | implemented / see B1 | `tests/test_ids.py`; deleted-video error path tested in `test_cli.py` + `integration/test_live.py:62`; extended-field mapping unit test missing (packet B1) |
| 2 discovery against committed flat fixture; integration vs a stable channel | implemented | `flat_entries.json` fixture; `tests/integration/test_live.py` |
| 3 captions: json3 preferred, VTT fallback, manual-over-auto, rolling dedup, window | implemented | `tests/test_captions.py` (39), `tests/test_transcripts.py` (18) |
| 4 cache: second call `cached: true` with zero yt-dlp calls; atomic writes | implemented | `tests/test_cache.py` (25; monkeypatched yt-dlp entry point); `_write_atomic` `cache.py:88` |
| 5 ASR: model resolution exactly per original §4; 16 kHz mono; cleanup; `-m slow` | implemented | `tests/test_asr.py` (15) + `tests/integration/test_asr_slow.py`; `asr.py` (env override → HF-cache scan preferring `large-v3-turbo` → named error `mlx-community/whisper-large-v3-turbo`; resolved model logged once, `asr.py:149-151`) |
| 6 hardening: full error taxonomy; `YT_MCP_REQUEST_DELAY` (default 1.0 s); `--version`; README client config (`claude mcp add` + opencode); mixed-list smoke | implemented | `errors.py:14-23` (8 plan codes + `internal_error`), `tests/test_errors.py`; `ytdlp.py:32-39`; README lines 79/86; `tests/integration/test_live.py` |

## 6. Strategy-lab layer (PLAN §6)

| item | status | evidence |
| --- | --- | --- |
| `.claude/skills/yt-research/SKILL.md` | implemented (existence) | present; contents not reviewed under this brief |
| `.claude/skills/strategy-spec/SKILL.md` + template + rubric + testability mapping | implemented (existence) | present (`strategy-lab/templates/strategy-spec.md`, `strategy-lab/rubric/flaws.md`); contents not read — the brief forbids strategy-lab reads, so the rubric/checklist items are neither verified nor falsified here |

## 7. Original-plan items carried forward (ORIGINAL §2-§5)

| item | status | evidence |
| --- | --- | --- |
| two-layer retrieval, `mode` ∈ auto/captions_only/asr_only, `captions_only` fails rather than falling back | implemented | `transcripts.py`; tested in `tests/test_transcripts.py`/`test_pipeline.py` |
| json3 preferred, VTT fallback, manual over auto, `caption_kind` recorded | implemented | `captions.py`; `tests/test_captions.py` |
| missing language → available languages in the error details | implemented | `transcripts.py:180-194`, `captions.py::available_languages` |
| model resolution order (env → HF cache scan → named error, resolved path, logged once) | implemented | `asr.py:117-151`; `tests/test_asr.py` |
| audio to 16 kHz mono WAV, deleted after transcription | implemented | `asr.py:169-185` |
| cache keyed `(video_id, lang, source)`, blobs + SQLite index, atomic writes, no TTL, `refresh` | implemented | `cache.py`; `tests/test_cache.py` |
| error taxonomy incl. `rate_limited`; request delay, no tight-loop retry | implemented | `errors.py`, `ytdlp.py:32-39`; `tests/test_ytdlp.py` (13), `tests/test_errors.py` |
| non-goals (no summarisation, no diarization in v1, no web UI, audio deleted) | respected | no such code exists in the package |

## 8. Beyond the plan (not gaps — recorded for completeness)

* `src/research_library/` + the `rlib` CLI + ~13 more MCP tools: an entire
  second research corpus (arXiv, RSS, Substack, PDF, StackExchange, podcast,
  slideslive adapters) with its own test fleet (`tests/test_library_*.py`).
  Neither plan document mentions it; it postdates both.
* `mcp >= 2.0` `MCPServer` fallback in `mcp_server.py`.
* Cache root renamed to `~/.cache/yt-research` (plan said
  `~/.cache/yt-transcript-mcp`) — consistent with the package rename,
  overridden via `YT_MCP_CACHE_DIR` as planned.
* Programme tooling (`scripts/board_audit.py`, `capacity_report.py`,
  `programme_status.py`, dispatchers) — governed by the handoff briefs, not
  by `PLAN.md`.

## 9. Rows that could not be classified

* Strategy-lab §6 content claims (what the skills/templates/rubric actually
  contain) — existence verified only; the brief forbids reading
  `strategy-lab/`.
* The plan's phase-0 acceptance ("server advertises all 7 tools") is judged
  against the modern 20-tool surface; the original 7 are a subset, so the
  row is recorded as implemented-and-superseded rather than counted as a
  mismatch.

**Net: the only actionable gap found is B1 (packet below); everything else
in both plan documents is implemented, implemented-and-superseded by planned
growth, or deliberately outside this review's reach.**
