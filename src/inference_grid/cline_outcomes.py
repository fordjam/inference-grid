"""Pure Cline classification; no dispatch, retry, quota release or acceptance."""

STOP_REASONS = {"visible_output_budget", "iteration_budget", "log_byte_budget", "wall_deadline"}


def classify_cline(events, supervisor, expected_model):
    """Supervisor must be controller-owned. Native completion is not acceptance."""
    result = {
        "outcome": "unqualified",
        "reason": "invalid_input",
        "receipt": None,
        "progress": {"iterations_observed": 0, "output_tokens_reported": None},
    }
    if not isinstance(events, list) or any(not isinstance(e, dict) for e in events):
        return result
    for row in events:
        event = row.get("event")
        if isinstance(event, dict) and event.get("type") == "iteration_start":
            result["progress"]["iterations_observed"] += 1
        if isinstance(event, dict) and event.get("type") == "usage":
            value = event.get("totalOutputTokens")
            if type(value) is int and value >= 0:
                previous = result["progress"]["output_tokens_reported"]
                result["progress"]["output_tokens_reported"] = max(previous or 0, value)
    if not isinstance(supervisor, dict):
        return result
    reason = supervisor.get("reason")
    if isinstance(reason, str) and reason in STOP_REASONS:
        result.update(outcome="interrupted", reason=reason)
        return result
    if reason != "process_exited":
        result["reason"] = "supervision_unqualified"
        return result
    if type(supervisor.get("returncode")) is not int or supervisor["returncode"] != 0:
        result["reason"] = "process_failed"
        return result
    finals = [i for i, row in enumerate(events) if row.get("type") == "run_result"]
    if len(finals) != 1:
        result["reason"] = "missing_or_multiple_terminals"
        return result
    if finals[0] != len(events) - 1:
        result["reason"] = "events_after_terminal"
        return result
    final = events[finals[0]]
    if final.get("finishReason") != "completed":
        result["reason"] = "native_not_completed"
        return result
    model = final.get("model")
    if (
        not isinstance(expected_model, str)
        or not expected_model.strip()
        or not isinstance(model, dict)
        or model.get("provider") != "cline"
        or not isinstance(model.get("id"), str)
        or model["id"] != expected_model
    ):
        result["reason"] = "model_unqualified"
        return result
    text = final.get("text")
    if not isinstance(text, str) or not text.strip():
        result["reason"] = "empty_terminal_text"
        return result
    result.update(
        outcome="native_complete",
        reason="verified_native_shape",
        receipt={"actual_model": model["id"], "finish_reason": "stop", "text": text},
    )
    return result
