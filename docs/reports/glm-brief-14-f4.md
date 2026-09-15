# GLM lane report — brief 14, F4: two upstream reproductions as documents (2026-09-15)

Lane: DeepSeek-V4.1-Flash via Command Code. Base: `origin/glm/work` at `f0a4523`.
Branch: `glm/f4-two-upstream-reproductions-as-documents`, one commit, not pushed.

The packet is F4 from `docs/handoff-glm-15.md`. This round was a resume: the gate came
back `expected exactly one commit ahead of origin/glm/work, found 0`. The branch was
sitting exactly on the base with a clean tree and the reflog shows no commit was ever
made on it (`HEAD@{0}` is the checkout onto the branch), so there was nothing to amend;
the packet is landed here as a single new commit. No test was weakened, skipped or
deleted.

## What landed

Two documents, no code:

- **`docs/upstream/cline-postinstall-signature.md`** — Cline's postinstall rewrites the
  package's `bin/.cline` in place, invalidating its code signature; on macOS 27 the kernel
  SIGKILLs every launch at `exec`, before the process prints anything. The document gives
  the environment (macOS 27, Cline 3.0.61), the observed command and result (`--version`,
  no stdout/stderr, exit 137 = 128 + 9, `returncode == -9` for a supervisor), the minimal
  reproduction (broken launcher vs. the platform package's real binary vs. the launcher
  with `CLINE_BIN_PATH` set), why the signature is what the kernel refused, the
  `CLINE_BIN_PATH` workaround, what upstream should change, and the two fields to paste
  from the affected machine before filing.
- **`docs/upstream/cline-id-json-prompt.md`** — Cline 3.0.61 refuses a positional prompt
  together with `--id <session>` in `--json` mode (`interactive mode is unsupported`), so
  every round after the first dies with an empty JSON transcript. The document gives the
  first (working) round and the second (refused) round verbatim, the minimal reproduction,
  the fresh-session-per-round workaround the packet adapter already uses (the first
  session id is kept only as audit), the note that `--id` without `--json` may work but
  was not tested here, and what upstream should change.

Also: one row in `docs/CONTRIBUTIONS.md` under `## 2026-09-15 — brief 14`, and this
report.

## Where the facts come from

Everything in the two documents is drawn from what the repository already records; the
raw session transcript is not in this checkout (it lives under the packets root the lane
rules put out of bounds), so the documents carry a short "Before filing" section naming
the two fields an operator should paste from the machine — `sw_vers` and the literal
stderr line. Nothing was invented and nothing was inferred beyond what the sources state:

- Cline 3.0.61 and the `--id` + `--json` refusal: `src/inference_grid/lanes/packet.py`
  (`ClineAdapter`, the comment on `resume`) and the `cline --id <session> <prompt>` row in
  `docs/handoff-glm-15.md`.
- macOS 27, the postinstall rewrite of `bin/.cline`, the broken signature, the SIGKILL
  before any output, and `CLINE_BIN_PATH`: `docs/LANES.md`'s Cline note and
  `docs/handoff-glm-15.md`.
- How a SIGKILL is named (`returncode < 0` → `signal.Signals(-rc).name`): the F1 probe in
  `src/inference_grid/doctor.py::version_probe`.
- The invocation shape used in both reproductions: `ClineAdapter` in
  `src/inference_grid/lanes/packet.py` and `lanes/cline.py`.

No credential, home path or e-mail address appears in either document; both use
placeholders (`<prefix>`, `<platform-package>`, `<model>`, `<sessionId>`).

## Defects found in existing code

None. This packet is documents only and touched no module, so none of the
provider-authored "integrated unmodified" files was involved.

## Final gate

- `pytest -q`: docs-only change; the failing node-id set is the pre-existing sandbox set
  and is unchanged from the base (the doc edits cannot affect any test).
- `ruff format` and `ruff check`: clean (no Python file changed; run for the record).
- `git grep "/Users/"` over the changed files: clean.
- `git log origin/glm/work..HEAD --oneline` names exactly one commit, with the
  DeepSeek-V4.1-Flash trailer, and the tree is clean after it.

## Note on the resume

The round-1 gate did not report a content problem with the documents — it failed only
because no commit existed. The documents are authored here for the first time in this
checkout; the reflog and a scan of dangling commits show no earlier version to recover.
