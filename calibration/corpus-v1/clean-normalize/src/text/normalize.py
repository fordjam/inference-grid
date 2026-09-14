"""Whitespace normalisation for imported survey headings."""


def normalize_heading(text: str) -> str:
    """Trim a survey heading and drop one trailing colon, if present."""
    trimmed = text.strip()
    if trimmed.endswith(":"):
        trimmed = trimmed[:-1].rstrip()
    return trimmed
