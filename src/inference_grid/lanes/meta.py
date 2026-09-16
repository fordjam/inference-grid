"""The lanes-meta sidecar: lane facts that are policy, not transport.

`lanes/config.py` (provider-authored, integrated unmodified) admits exactly its required
keys, so a lane's hosting and retention guarantees cannot live in `lanes.json`. They live
beside it in `lanes-meta.json`, kept by the operator:

    {"lanes": {"go-opencode": {"residency": "us", "retention": "zero",
                               "retention_source": "https://opencode.ai/docs/zen"}}}

`residency` is one of us | eu | unknown, `retention` one of zero | days | unknown, and
`retention_source` the URL or note the two facts come from. A lane the file does not name
reads as unknown on every key — the same reading a missing file gives every lane — so a
board that requires guarantees refuses untagged lanes instead of trusting them. A
malformed sidecar (unparseable JSON, wrong shape, a value outside the enums) raises and
the caller refuses the whole tick: fail closed, never route on half-read policy.
"""

import json
from pathlib import Path

FILENAME = "lanes-meta.json"

# The keys a lane record may carry and, for the two enumerated ones, their values.
# `retention_source` is free text — a URL or a note. A record may carry any subset:
# an absent key reads unknown, exactly like an absent lane.
ENUMS = {
    "residency": ("us", "eu", "unknown"),
    "retention": ("zero", "days", "unknown"),
}
SOURCE_KEY = "retention_source"

# The value an absent key or an absent lane reads as; it is deliberately not a value a
# requirement can list — see validate_requirement.
UNKNOWN = "unknown"


def validate_lane_meta(raw):
    """The sidecar document as written: {"lanes": {lane_id: record}}; ValueError otherwise."""
    if not isinstance(raw, dict) or set(raw.keys()) != {"lanes"}:
        raise ValueError("lanes-meta: expected a dict with exactly one key 'lanes'")
    lanes = raw["lanes"]
    if not isinstance(lanes, dict):
        raise ValueError("lanes-meta: 'lanes' must be a dict")
    out = {}
    for lane_id, record in lanes.items():
        if not isinstance(lane_id, str) or not lane_id:
            raise ValueError("lanes-meta: lane id must be a non-empty str")
        if not isinstance(record, dict):
            raise ValueError(f"lanes-meta: {lane_id} must be a dict")
        unknown_keys = set(record.keys()) - set(ENUMS) - {SOURCE_KEY}
        if unknown_keys:
            raise ValueError(
                f"lanes-meta: {lane_id} carries unknown key(s) "
                + ", ".join(sorted(unknown_keys))
                + f"; allowed: {', '.join(sorted(set(ENUMS) | {SOURCE_KEY}))}"
            )
        for key, values in ENUMS.items():
            if key in record and record[key] not in values:
                raise ValueError(
                    f"lanes-meta: {lane_id}: {key} must be one of " + ", ".join(values)
                )
        if SOURCE_KEY in record and (
            not isinstance(record[SOURCE_KEY], str) or not record[SOURCE_KEY].strip()
        ):
            raise ValueError(f"lanes-meta: {lane_id}: {SOURCE_KEY} must be a non-empty str")
        out[lane_id] = dict(record)
    return {"lanes": out}


def load_lane_meta(lanes_path):
    """The sidecar records beside `lanes_path`, as {lane_id: record}; {} without a file.

    Read once per tick. An absent file is the common case — no operator has tagged any
    lane — and reads as no records, never an error. A malformed one raises with the
    parse error, and the tick that called this refuses (fail closed).
    """
    if not lanes_path:
        return {}
    path = Path(lanes_path).parent / FILENAME
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise ValueError(f"lanes-meta: {path} does not parse: {exc}") from None
    return validate_lane_meta(raw)["lanes"]


def validate_requirement(require):
    """The tick config's `require_lane_meta`, checked; ValueError on junk.

    `{"residency": ["us", "eu"], "retention": ["zero"]}` — a dict of sidecar key to the
    non-empty list of values the board accepts. A key the sidecar does not carry, an
    empty or non-list value, or `unknown` among the allowed values refuses here: unknown
    never satisfies, so listing it could only widen the gate back open by accident.
    """
    if not isinstance(require, dict) or not require:
        raise ValueError("require_lane_meta: expected a non-empty dict of key -> allowed values")
    for key, allowed in require.items():
        if key not in ENUMS:
            raise ValueError(
                f"require_lane_meta: unknown key {key!r}; allowed: " + ", ".join(sorted(ENUMS))
            )
        if not isinstance(allowed, list) or not allowed:
            raise ValueError(f"require_lane_meta: {key} must be a non-empty list of values")
        if any(value == UNKNOWN for value in allowed):
            raise ValueError(
                f"require_lane_meta: {key}: unknown never satisfies, it cannot be allowed"
            )
        if any(not isinstance(value, str) or not value for value in allowed):
            raise ValueError(f"require_lane_meta: {key} takes non-empty strings")
        outside = [value for value in allowed if value not in ENUMS[key]]
        if outside:
            raise ValueError(
                f"require_lane_meta: {key}: {', '.join(outside)} not one of "
                + ", ".join(ENUMS[key])
            )
    return require


def policy_drop(lane_id, record, require):
    """The drop row for the first listed key the lane's record fails, else None.

    A lane satisfies a board requirement only when every listed key's sidecar value is
    among the allowed ones; an absent key or an untagged lane reads unknown and never
    satisfies. The row names the key, so a dry run says which guarantee was missing.
    """
    record = record if isinstance(record, dict) else {}
    for key, allowed in require.items():
        value = record.get(key, UNKNOWN)
        if value not in allowed:
            return {
                "lane": lane_id,
                "reason": "lane_policy",
                "detail": f"{key} {value} not in [{', '.join(allowed)}]",
            }
    return None
