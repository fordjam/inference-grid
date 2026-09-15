# Cline CLI on macOS 27: postinstall rewrites `bin/.cline`, the kernel SIGKILLs every launch

Ready to paste as an upstream issue. No code, no credentials, no private paths.

## Summary

On macOS 27, Cline's own postinstall rewrites the package's `bin/.cline` launcher in
place. The rewrite invalidates the launcher's code signature, and the kernel then kills
every invocation with `SIGKILL` before the process can print anything. The CLI looks
installed and on `PATH` while every run — `--version` included — dies silently.

## Environment (as observed)

- macOS 27 (the upgrade that rebooted the machine; see "Before filing" for the exact
  `sw_vers` output).
- Cline CLI 3.0.61.
- Launcher: `<prefix>/bin/.cline`, the shim the package's postinstall rewrites.

## Observed

Smallest probe — the launcher on `PATH`:

```
$ cline --version
$ echo "exit=$?"
exit=137
```

No stdout. No stderr. The process is killed before it prints. The shell that launched it
reports the signal rather than the CLI's own error:

```
zsh: killed     cline --version
```

A supervisor reading the raw wait status sees the signal by name: Python's `subprocess`
returns `returncode == -9`, and `signal.Signals(9).name == "SIGKILL"`.

A real dispatch dies the same way:

```
$ cline --provider cline --model <model> --json "hello"
zsh: killed     cline ...
```

The JSON event stream the caller captures is empty, so the failure looks like an agent
that produced nothing rather than a binary the kernel refused to start. In the reporting
environment a scheduled lane spent about a day being SIGKILLed on every round before
anyone ran `--version`.

## Minimal reproduction

```
# 1. install the package so its own postinstall runs (rewrites bin/.cline)
<install command>

# 2. the rewritten launcher is killed at exec, before any output
<prefix>/bin/.cline --version; echo "exit=$?"     # exit=137, "zsh: killed"

# 3. the platform package's real binary runs fine
"<platform-package>/bin/cline" --version; echo "exit=$?"   # 3.0.61

# 4. and the launcher runs fine when pointed at the real binary first
CLINE_BIN_PATH="<platform-package>/bin/cline" <prefix>/bin/.cline --version; echo "exit=$?"
```

Step 3 shows the binary itself is intact; step 4 shows the launcher is the broken part
and that `CLINE_BIN_PATH` is the seam the launcher honours first.

## Why

macOS requires a valid code signature for the executables it starts. A postinstall that
edits a signed file in place leaves the signature stale, and the kernel kills the process
at `exec` — before `main`, before any stdout, before the CLI can log anything itself. This
is why there is no error text from Cline: the process never runs.

## Workaround

Point the launcher at the platform package's real binary. The launcher honours
`CLINE_BIN_PATH` first, so the postinstall's rewrite of its own file stops mattering:

```
export CLINE_BIN_PATH="<platform-package>/bin/cline"
cline --version     # 3.0.61
```

The calling tool should also probe the binary (`<executable> --version`) and report a
signal-killed probe as a named failure, so the next occurrence is a finding rather than
silence.

## What upstream should change

- The postinstall should not rewrite the launcher in place. Write a fresh file (which gets
  a fresh signature) or re-sign what it rewrote (ad-hoc is enough for a local CLI).
- `--version` failing with a signal and no output should be surfaced as a broken install,
  not left for the caller to discover a day later.

## Before filing

Paste these from the affected machine (they are not captured in this repository):

```
sw_vers                 # exact macOS build
<prefix>/bin/.cline --version; echo "exit=$?"
"<platform-package>/bin/cline" --version
```

Redact the install prefix and any account name before posting.
