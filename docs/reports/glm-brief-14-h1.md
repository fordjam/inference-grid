# GLM lane report — brief 14, H1: board_prepare configures the goat and cline accounts (2026-09-15)

Lane: GLM-5.3-Flash via Command Code. Base: `origin/glm/work` at `a93c5b5`.
Branch: `glm/h1-board-prepare-py-configures-goat-account`, one commit, not pushed.

The packet is H1 from `docs/handoff-glm-15.md` (Phase H): implement exactly what
`docs/LANES.md`'s board-prepare paragraph says for the `goat` and `cline` accounts.

## What landed

`deployments/local/board_prepare.py`:

- **`configure_observation` — one observation file, one account, one record per lane.**
  Given an observation, window unit caps and a freshness window, it reads the
  observation's `used_percent` per window, computes `remaining = cap × (100 − used) / 100`,
  and calls `ledger.configure_account(account, 1, remaining, observed + valid, models,
  lane_ids, observed_at=observed)`. It then records one lane record per configured lane:
  `auth: "ok"`, `quota_observed_at`, `quota_freshness_seconds`, `used_percent_max` (max
  across windows), `admission_limit_percent` 80, `qualification: "qualified"` — the same
  shape the zai/go records already use.
- **`goat-account` from `goat-observation.json`** (`collect_goat.py`): caps 14 / 35 / 70
  credits for `five_hour` / `weekly` / `monthly`, freshness 900 s.
- **`cline` from `cline-observation.json`** (`collect_cline.py`): the same three windows
  with no published unit caps, so each window reads against a 100-unit scale
  (`remaining = 100 − used`), freshness 900 s.
- **Lane ids and models come from the config only.** `goat_lanes` / `cline_lanes` are lists
  of `{lane, model}`; the account is configured with the distinct models in lane order and
  `alias_names` is the lane ids. With the key absent (or an empty list) the account is left
  completely alone — `lanes.json` is never read.
- **Stale or non-ok leaves the account untouched and the lanes stale.** A missing
  observation file, a `status` other than `ok`, or an observation older than its freshness
  window skips `configure_account` entirely; the lane record carries
  `quota_observed_at: None` (non-ok / missing) or the real timestamp (stale), so the
  ledger's own `lane_readiness` classifier names the state `stale` (`quota_unobserved` /
  `quota_stale`).
- Both run from `configure()` after the zai/go work; observation paths are
  `goat_observation_path` / `cline_observation_path` in the config with the
  `inference-grid-capacity` directory as default, matching the existing config style.

The operator's running copy under `~/.local/share/inference-grid-capacity/` was **not**
ported — per the packet, the operator cuts over to this versioned file.

## Tests

`tests/test_local_board_prepare.py` (7 tests, offline; fixture observations in a temp dir,
a fake ledger capturing the exact calls, stale-ness judged by the real `lane_readiness`
classifier through `classifier_view`):

- goat fixture → the exact `configure_account` call (remaining 10.5 / 21 / 35 against the
  14 / 35 / 70 caps, `expires = observed + 900`, models and `alias_names` from the config)
  and one record per lane, each classified `ready`.
- cline fixture → the 100-unit scale (`remaining` 87.5 / 56 / 91), account id `cline`.
- stale observation (`observed_at` past `observed + 900`) → no `configure_account` call,
  records carry the real timestamp and classify `quota_stale`.
- non-ok observation → no call, `quota_observed_at: None`, classifies `quota_unobserved`.
- absent `goat_lanes` / `cline_lanes` → nothing called at all.
- missing observation file → no call, lanes still recorded stale.
- both observations read through their config paths end to end via `configure_goat` /
  `configure_cline`.

## Defects found in existing code

None. `board_prepare.py` is a `deployments/local/` file, not one of the provider-authored
"integrated unmodified" modules, so no caller-vs-module split was needed. One behavioural
note: `collect_goat.py` emits `status: "ok"` even when the monthly credit reading is absent
(two windows instead of three); `configure_observation` configures from whatever windows
are present rather than requiring all three, so a missing monthly reading still admits the
lane against its five-hour and weekly windows. ClinePass's collector already refuses `ok`
until all three windows answered.

## Final gate

- `pytest -q`: **88 failed, 472 passed, 7 skipped** (+7 new tests). The 88 failures are the
  pre-existing sandbox/`killpg` set; the failing node-id list was diffed against a scratch
  worktree at the base `a93c5b5` and is **byte-identical** (88 inherited, 0 new).
- `ruff format` and `ruff check` on `deployments/local/board_prepare.py` and
  `tests/test_local_board_prepare.py`: clean.
- No absolute home paths added (defaults stay `Path.home()`-derived).
- `git log origin/glm/work..HEAD --oneline` names exactly one commit, with the
  `Co-Authored-By: GLM-5.3-Flash <noreply@z.ai>` trailer.

## Not verified here, and the operator step

- No live observation files or ledger were touched (the board runner owns them); every
  fixture lives in a temp directory and the ledger is a fake.
- Operator step: copy the deployed `board_prepare.py` over the running copy, add
  `goat_lanes` / `cline_lanes` to `~/.local/share/inference-grid-capacity/config.json`
  (e.g. `[{"lane": "goat", "model": "glm-5.3-flash"}]`), and let `collect_goat.py` /
  `collect_cline.py` run once so the observation files exist; the next `board_prepare`
  run then admits both lanes from live readings.
