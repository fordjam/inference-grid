"""Validate terminal adapter receipts; receipt claims are not review approval."""

import re
from pathlib import PurePosixPath


def safe_path(value):
    return (
        isinstance(value, str)
        and bool(value)
        and "\\" not in value
        and "\x00" not in value
        and not re.match(r"^[A-Za-z]:", value)
        and not PurePosixPath(value).is_absolute()
        and all(part not in ("", ".", "..") for part in value.split("/"))
    )


def sha256(value):
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def validate_receipt(receipt, expected_model, expected_manifest_sha256):
    if not isinstance(receipt, dict):
        return ["receipt must be an object"]
    errors = []
    for field, expected in (
        ("status", "completed"),
        ("finish_reason", "stop"),
        ("actual_model", expected_model),
        ("manifest_sha256", expected_manifest_sha256),
    ):
        if receipt.get(field) != expected:
            errors.append(field + " mismatch")
    if not sha256(receipt.get("manifest_sha256")):
        errors.append("invalid manifest digest")
    artifacts = receipt.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        return errors + ["nonempty artifact list required"]
    seen = set()
    for item in artifacts:
        if not isinstance(item, dict):
            errors.append("invalid artifact")
            continue
        path = item.get("path")
        if not safe_path(path):
            errors.append("unsafe artifact path")
        elif path in seen:
            errors.append("duplicate artifact path")
        else:
            seen.add(path)
        if not sha256(item.get("sha256")):
            errors.append("invalid artifact digest")
    return errors
