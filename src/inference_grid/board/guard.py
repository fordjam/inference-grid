"""Refuse to stage inputs that look like credentials or private data before they reach a provider.

Board inputs are sent to third-party models by design; this guard is the last check that the
listed files are code and text, not secrets. It is deliberately conservative: a false refusal
costs one task edit, a false pass costs a credential.
"""

import re
from pathlib import PurePosixPath

DENIED_NAMES = re.compile(
    r"(^|/)(\.env(\..*)?|auth\.json|credentials?(\..*)?|secrets?(\..*)?|lanes\.json|"
    r"zai-coding-plan\.json|id_(rsa|ed25519|ecdsa)(\.pub)?|.*\.(pem|key|p12|pfx|keychain|kdbx)|"
    r".*token.*\.json|.*\.sqlite(3)?|.*\.db)$",
    re.IGNORECASE,
)
SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[abp]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._-]{24,}"),
    re.compile(
        r"(?i)\b(api[_-]?key|secret|token|password|passwd)\b\s*[:=]\s*['\"]?[A-Za-z0-9._/+-]{20,}"
    ),
]
MAX_BYTES = 2_000_000


def check_name(name):
    if DENIED_NAMES.search(str(PurePosixPath(name))):
        return "name looks like a credential or private store"
    return None


def check_content(data):
    if len(data) > MAX_BYTES:
        return "input larger than the staging limit"
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return "binary input is not staged"
    for pattern in SECRET_PATTERNS:
        if pattern.search(text):
            return "content matches a credential pattern: " + pattern.pattern[:40]
    return None


def check_input(name, data):
    """Return None when the input may be staged, else a short reason."""
    return check_name(name) or check_content(data)
