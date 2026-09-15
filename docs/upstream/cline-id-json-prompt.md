# Cline CLI: a prompt with `--id <session>` in `--json` mode is refused ("interactive mode is unsupported")

Ready to paste as an upstream issue. No code, no credentials, no private paths.

## Summary

Cline CLI 3.0.61 cannot resume a session and submit a new prompt in one headless
invocation. `--id <session>` together with `--json` and a positional prompt is refused, so
every round after the first dies. There is no documented flag combination that both
re-enters a session and runs a prompt non-interactively.

## Environment (as observed)

- macOS 27.
- Cline CLI 3.0.61.
- Headless invocation: `--json --auto-approve true`, prompt as the positional argument,
  per-run state under `--data-dir`.

## Observed

First round (fresh session) works and its JSON stream names the session:

```
$ cline --provider cline --model <model> --json --auto-approve true \
    --thinking high --retries 8 --cwd <work> --data-dir <state> \
    "first prompt"
# -> events; the final document carries the session id
```

Second round (resume the same session with a prompt) is refused:

```
$ cline --provider cline --model <model> --json --auto-approve true \
    --thinking high --retries 8 --cwd <work> --data-dir <state> \
    --id <sessionId> "second prompt"
# -> refused: interactive mode is unsupported
```

The CLI rejects the combination with `interactive mode is unsupported`. In the reporting
environment every round after the first died inside the round's 20 s budget, with an empty
JSON transcript, so a packet loop that could re-enter a session on other CLIs could not on
Cline.

## Minimal reproduction

```
# 1. fresh session, capture the id from the JSON output (sessionId / session_id /
#    taskId / task_id)
cline --provider cline --model <model> --json --auto-approve true \
  --cwd <work> --data-dir <state> "first prompt" > first.jsonl

# 2. resume with a prompt in JSON mode — this is the refusal
cline --provider cline --model <model> --json --auto-approve true \
  --cwd <work> --data-dir <state> --id <sessionId> "second prompt"
```

## Workaround

- Run each round as a fresh session and carry context in the prompt. Do not reuse the
  session id to submit a later prompt. The reporting tool does exactly this: every round
  builds the same fresh-session argv, and the first session's id is read from the first
  round's transcript and kept only as an audit record.
- The refusal names JSON mode specifically. `--id` with a prompt outside `--json` may
  still work, because the CLI is then allowed to be interactive — untested here, and it
  gives up the machine-readable event stream the caller classifies.

## What upstream should change

- Accept a prompt with `--id` in `--json` mode: resuming a session is not inherently
  interactive, and a headless caller has no stdin to offer.
- If that combination is intentionally unsupported, name the supported headless resume
  form in the error. `interactive mode is unsupported` does not say which flag made the
  call interactive, and a caller cannot tell it from "nothing to resume".

## Before filing

Paste these from the affected machine (they are not captured in this repository):

```
sw_vers
cline --version
cline --id <sessionId> --json "second prompt"   # exact stderr line
```

Redact the work and state directories and the session id before posting.
