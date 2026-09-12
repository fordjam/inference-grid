"""Structural validation for LLM receipt dicts.

An empty error list means the receipt is STRUCTURALLY well formed only.
It is NOT task acceptance: content correctness is never judged here.
"""


def receipt_errors(value, expected_model) -> list[str]:
    """Return structural problems with ``value``; empty list means shape is OK."""
    if not isinstance(value, dict):
        return ["receipt must be a dict"]
    errors = []
    want = expected_model
    if not isinstance(want, str) or not want.strip():
        errors.append("expected_model must be a non-empty string")
    actual = value.get("actual_model")
    if not isinstance(actual, str):
        errors.append("actual_model must be a string")
    elif isinstance(want, str) and actual != want:
        errors.append("actual_model %r != expected_model %r" % (actual, want))
    if value.get("finish_reason") != "stop":
        errors.append("finish_reason must equal 'stop'")
    text = value.get("text")
    if not isinstance(text, str):
        errors.append("text must be a string")
    elif not text.strip():
        errors.append("text must be non-empty after stripping")
    return errors
