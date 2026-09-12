"""Strict JSON transport decoding; callers must validate the resulting schema."""

import json
import math
import re

_FENCE = re.compile(r"\A```(?:json)?\r?\n(.*?)\r?\n```\Z", re.DOTALL)


def parse_json_object(text, max_chars=100000):
    """Accept an object or one enclosing JSON fence, never surrounding commentary.

    The character bound includes whitespace and fences. No schema is implied.
    Duplicate keys and nonfinite numbers are refused, including nested values.
    """
    if type(max_chars) is not int or max_chars < 1:
        raise ValueError("max_chars must be a positive integer")
    if not isinstance(text, str) or len(text) > max_chars:
        raise ValueError("text required within character bound")
    content = text.strip()
    if content.startswith("```"):
        match = _FENCE.fullmatch(content)
        if match is None:
            raise ValueError("exactly one enclosing JSON fence required")
        content = match.group(1)
        if "```" in content:
            raise ValueError("multiple or nested fences refused")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def constant(_):
        raise ValueError("nonfinite JSON number")

    def finite_float(value):
        result = float(value)
        if not math.isfinite(result):
            raise ValueError("nonfinite JSON number")
        return result

    try:
        result = json.loads(
            content, object_pairs_hook=pairs, parse_constant=constant, parse_float=finite_float
        )
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("invalid JSON object") from exc
    if not isinstance(result, dict):
        raise ValueError("top-level JSON object required")
    return result
