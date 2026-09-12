"""Native GOAT (Command Code) outcome normalizer. Authored by OpenCode Go (glm-5.3-flash) through a
Grid attempt; integrated unmodified after local tests. See docs/CONTRIBUTIONS.md."""

def classify_goat(events, supervisor, expected_model):
    result = {
        "outcome": "unqualified",
        "reason": "invalid_input",
        "receipt": None,
        "progress": {"model_requests": 0, "output_tokens_reported": None},
    }
    if not isinstance(events, list):
        return result
    if not all(isinstance(row, dict) for row in events):
        return result
    progress = result["progress"]
    mres = []
    for row in events:
        if row.get("type") != "event":
            continue
        event = row.get("event")
        if not isinstance(event, dict):
            continue
        if event.get("type") != "model_request_end":
            continue
        mres.append(event)
        progress["model_requests"] += 1
        usage = event.get("usage")
        if isinstance(usage, dict):
            out_tokens = usage.get("outputTokens")
            if isinstance(out_tokens, int) and not isinstance(out_tokens, bool) and out_tokens >= 0:
                if progress["output_tokens_reported"] is None:
                    progress["output_tokens_reported"] = 0
                progress["output_tokens_reported"] += out_tokens
    if not isinstance(supervisor, dict):
        return result
    supervisor_reason = supervisor.get("reason")
    if supervisor_reason in ("wall_deadline", "visible_output_budget", "iteration_budget", "log_byte_budget"):
        result["outcome"] = "interrupted"
        result["reason"] = supervisor_reason
        return result
    if supervisor_reason != "process_exited":
        result["reason"] = "supervision_unqualified"
        return result
    returncode = supervisor.get("returncode")
    if not (isinstance(returncode, int) and not isinstance(returncode, bool) and returncode == 0):
        result["reason"] = "process_failed"
        return result
    terminal_indices = [i for i, row in enumerate(events) if row.get("type") == "result"]
    if len(terminal_indices) != 1:
        result["reason"] = "missing_or_multiple_terminals"
        return result
    if terminal_indices[0] != len(events) - 1:
        result["reason"] = "events_after_terminal"
        return result
    final = events[terminal_indices[0]]
    if final.get("subtype") != "success" or final.get("stopReason") != "end_turn":
        result["reason"] = "native_not_completed"
        return result
    if not (isinstance(expected_model, str) and expected_model != ""):
        result["reason"] = "model_unqualified"
        return result
    if not mres or any(event.get("model") != expected_model for event in mres):
        result["reason"] = "model_unqualified"
        return result
    final_text = final.get("finalText")
    if not (isinstance(final_text, str) and final_text.strip() != ""):
        result["reason"] = "empty_terminal_text"
        return result
    efforts = set()
    for event in mres:
        value = event.get("effort")
        if isinstance(value, str):
            efforts.add(value)
    effort = next(iter(efforts)) if len(efforts) == 1 else None
    final_usage = final.get("usage")
    usage_out = None
    if isinstance(final_usage, dict):
        in_tokens = final_usage.get("inputTokens")
        out_tokens = final_usage.get("outputTokens")
        if (
            isinstance(in_tokens, int) and not isinstance(in_tokens, bool) and in_tokens >= 0
            and isinstance(out_tokens, int) and not isinstance(out_tokens, bool) and out_tokens >= 0
        ):
            usage_out = final_usage
    result["outcome"] = "native_complete"
    result["reason"] = "verified_native_shape"
    result["receipt"] = {
        "actual_model": expected_model,
        "finish_reason": "stop",
        "text": final_text,
        "effort": effort,
        "usage": usage_out,
    }
    return result
