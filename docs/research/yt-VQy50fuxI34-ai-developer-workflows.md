# "Forget loop engineering" — AI developer workflows (IndyDevDan, 2026-07-13)

Source: `[yt:VQy50fuxI34]`, 34 min, auto-captions, cached in yt-research on
2026-09-14. Extracted for inference-grid; the video is about coding-agent
orchestration in general, not trading. Summary in our words; timestamps cite
the transcript.

## The thesis (00:00–04:50)

"Loop engineering" (the agent-runs-until-the-linter-passes idea) is a rebrand
of one control-flow construct of the software development lifecycle. The
useful frame is the **AI developer workflow (ADW)**: a fixed pipeline of
nodes where each node is one of three **actors of value creation** —
engineers, agents, or plain code — placed deliberately. Code is the cheapest
and most reliable actor (no tokens, deterministic, fast); engineers next;
agents last. Loops are one construct inside such a workflow, not the unit of
design `[@ 03:30–04:50]`.

## The build-up, node by node (04:50–18:35)

1. Engineer prompts, agent works, engineer reviews — the atom `[@ 04:50]`.
2. Add a code node (lint) with a fail→back-to-build-agent edge — the first
   loop `[@ 05:29]`. Then formatter, type-check, tests as more code nodes
   feeding the same agent `[@ 06:14–07:09]`.
3. Observation: **the engineer appears only at the two ends** — planning
   (prompting) and validation (review). Everything between should be agents
   and code `[@ 07:09]`.
4. Collapse the validation nodes into a **test agent** that holds the
   test/lint/type-check context and sends failures back with context
   `[@ 07:40]`.
5. Add a plan node ahead of build `[@ 08:22]`.
6. **Isolation**: a code node fans the plan into git worktrees, one agent per
   worktree, in parallel `[@ 09:05–09:52]`. Then one better: **a sandbox per
   agent** (its own machine), so the reviewer can step in and look at the
   running result `[@ 10:35–11:04]`.
7. **Intake**: a ticket board (kanban) as the input surface; tickets from
   support/product/engineering. A **scout agent** gathers code, docs, prior
   specs, then hands to a **plan agent** — search and planning split into two
   agents `[@ 12:02–14:05]`. Code moves the ticket state between phases;
   build → test loop → CI/CD (fail routes back to build) → engineer review →
   ship `[@ 14:05–15:00]`.
8. **Incident workflow**: production down → ticket to chat → engineer prompts
   a scout → a specialised **hotfix agent** (a persona tuned to ship the fix,
   not to do it elegantly) → **human approval gate** → then **N sandboxes race
   the same fix in parallel, first passing solution wins**, budget-scaled
   `[@ 15:23–17:32]`.
9. **The factory**: many specialised workflows (chore, bug, feature, hotfix)
   behind a **router** — an LLM call or plain code — that picks the workflow
   and, critically, the **model tier**: workhorse/lightweight models for
   chores and build, state-of-the-art for scout and plan "so nothing gets
   missed" `[@ 17:57–21:12]`. Advanced teams drop the engineer-rewrites-the-
   ticket step and, eventually, the engineer review, once the system has
   earned it `[@ 13:04, 21:12]`.

## The three practical recommendations (26:45–31:59)

1. **Start with the smallest workflow, and separate code from agents from
   the start.** Not "a skill that runs lint at the end" — an SDK-driven build
   agent, then a real code node running the linter, failures fed back into
   the same agent session. Otherwise it is an agent calling code, and you
   cannot test or guard the nodes `[@ 27:00–28:04]`. Skill-only workflows are
   fine for prototyping; move steps into code before production `[@ 28:38]`.
2. **Design the workflow by doing it yourself first**, end to end, with your
   agent in the terminal — step into every node, watch every condition — then
   write it down (he uses a Mermaid diagram) before automating `[@ 29:07–30:09]`.
3. **Agents plus code beats either alone.** Code is where speed, reliability
   and testability come from; every node and edge (plan→build, build→status
   update, test→fail) needs its own test. Classic engineering — decoupled,
   isolatable, single-interface — matters more, not less, because the
   workflow runs thousands of times `[@ 30:09–31:59]`.

Also: the **agentic layer** (prompts, skills, agent definitions, the harness
that wraps the app) is where engineering time should go — "build the system
that builds the system"; template your specialised expertise into agents
("agent experts") rather than using out-of-the-box generalists `[@ 12:30,
22:09, 24:57–25:32]`.

## Where inference-grid already is this, and where it is not

| Video idea | inference-grid today | Gap |
|---|---|---|
| Sandbox per agent, reviewer can step in | `lanes/sandbox.py` profiles, deny-read lists, per-lane worktree; operator reads `attempts/<stamp>/` | ✓. Operator "stepping in" = re-running the gates on the worktree, which we do. |
| Code nodes between agents (lint/test as code, not as a skill step) | Board tasks carry `tests`; the runner runs them as code and settles on the result; `board-prepare.py` refreshes readiness as code | Partial: inside a *lane* the agent still runs pytest itself (a skill-shaped step). The video's point is to pull that out into a harness node that feeds failures back with the same session. `run_lane.py` is one shot; there is no build→test→build loop driven by code. |
| Engineer only at the two ends | Operator writes the brief; operator merges and re-gates | ✓ in shape. Reviews (Kimi) are the agent-side validation node. |
| Scout agent → plan agent split | None. The brief author (Claude) does both by hand. | A cheap scout (list files, prior reports, tests touched) before the packet is written would cut the "spec written blind" failures. |
| Ticket board as intake, code moves state | `grid/board/*.json` + `board-tick`; state transitions in code | ✓ — this is the closest match. |
| Router picks workflow **and model tier** by task kind | `lanes/select.py`: filter + acceptance rate per (family, model, category) | Same intent, coarser signal; now fed by `external` records too. No tiering rule like "planner = SOTA, build = workhorse". |
| Race N sandboxes, first pass wins | None | Fits the grid's design (ledger, capacity per account). Would need a "race" task kind that admits N attempts and accepts the first passing one, cancelling the rest. |
| Hotfix persona with a human approval gate | None | Not needed for monarch today. |
| Model tiering: SOTA for plan/scout, workhorse for build | Implicit: Claude writes briefs, GLM/DeepSeek build, Kimi reviews | ✓ in practice, not encoded. |
| Test every node and edge of the workflow | `tests/` covers ledger, runner, select, review authoring | The *lane* workflow (`run_lane.py`) has no tests at all. |
| Do it by hand first, then diagram | The monarch lanes grew out of hand-run packets (P1–P4) | ✓. A Mermaid of the current lane+board flow does not exist; worth drawing. |

## Concrete takeaways for the grid, in priority order

1. **Code-driven build→gate→build loop inside a lane.** Replace the one-shot
   `run_lane.py` with: agent builds → harness runs `pytest`/`tsc`/`build` as
   code → on failure, re-enter the *same session* with the failure output →
   bounded retries. This is the video's core recommendation and the one
   thing that would have caught T1's six spec bugs (if the harness could run
   Playwright — which is the operator-side gate today; see 3).
2. **Scout node before the brief.** A cheap agent call (or plain code:
   `git log`, touched tests, prior reports) that produces the file list and
   the "what is already done" section the brief currently gets from Claude
   by hand.
3. **Operator-gate flag on packets.** Where a gate cannot run in the sandbox
   (Playwright), mark the packet so the workflow routes it to a fix-pass
   rather than to "done" — the `verified_in_lane` receipt field is the first
   half of this; the routing half is not built.
4. **Race task kind** for small, well-specified fixes: N lanes, first green
   wins. Low priority for monarch's volume.
5. **Tests for `run_lane.py`** — sandbox profile, deny list, argv, verdict
   and external-skeleton writing. It is the most-run code in this setup and
   the only untested part.

Not adopted: dropping engineer review (the reviewer signal is not yet
trustworthy enough — 41 approvals, 2 rejections, both correct, but zero
findings on ~10k lines is under-critical); the hotfix persona.
