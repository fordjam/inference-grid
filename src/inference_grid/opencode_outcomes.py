"""Qualify an OpenCode run from the CLI's own JSON event stream, never from prose.

The stream is the controller evidence: every line must parse, exactly one terminal
`step_finish` may name the run's model and finish reason, and the assistant's last
`text` part is the terminal text. Event shapes assumed (documented for the operator to
re-check against a real capture; the repo's only captured line so far is the
`step_start`/`sessionID` one):

    {"type": "step_start", "sessionID": "ses_..."}
    {"type": "text", "text": "..."}
    {"type": "step_finish", "reason": "stop", "modelID": "...", "tokens": {...}}
"""

MAX_EVENTS = 10000


def classify_opencode(rows, supervisor, model):
    """Return {outcome, reason, progress, receipt?} for one parsed event stream."""
    progress = {"events": len(rows), "steps": 0}
    if supervisor is not None and supervisor.get("reason") == "wall_deadline":
        return {
            "outcome": "interrupted",
            "reason": "wall_deadline",
            "progress": progress,
            "receipt": None,
        }
    if len(rows) > MAX_EVENTS:
        return {
            "outcome": "malformed_stream",
            "reason": "event stream exceeds the event bound",
            "progress": progress,
            "receipt": None,
        }
    for row in rows:
        if not isinstance(row, dict):
            return {
                "outcome": "malformed_stream",
                "reason": "a native line is not a JSON object",
                "progress": progress,
                "receipt": None,
            }
        if row.get("type") == "step_start":
            progress["steps"] += 1
    terminals = [row for row in rows if row.get("type") == "step_finish"]
    if len(terminals) != 1:
        return {
            "outcome": "missing_or_multiple_terminals",
            "reason": f"{len(terminals)} step_finish events",
            "progress": progress,
            "receipt": None,
        }
    terminal = terminals[0]
    echoed = terminal.get("modelID")
    if not isinstance(echoed, str) or echoed.lower() != model.lower():
        return {
            "outcome": "model_unqualified",
            "reason": "terminal event names model " + repr(echoed),
            "progress": progress,
            "receipt": None,
        }
    texts = [row.get("text") for row in rows if row.get("type") == "text"]
    text = " ".join(t for t in texts if isinstance(t, str)).strip()
    if not text:
        return {
            "outcome": "empty_terminal_text",
            "reason": "no assistant text part in the stream",
            "progress": progress,
            "receipt": None,
        }
    return {
        "outcome": "native_complete",
        "reason": "one terminal step_finish naming the model",
        "progress": progress,
        "receipt": {
            "finish_reason": terminal.get("reason"),
            "actual_model": echoed,
            "text": text,
            "usage": terminal.get("tokens"),
        },
    }
