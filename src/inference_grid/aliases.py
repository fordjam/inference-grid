def canonical_account(name: str, aliases: dict[str, str]) -> str:
    def valid_id(v):
        return isinstance(v, str) and v != "" and v.strip() != ""

    if not isinstance(aliases, dict):
        raise ValueError("aliases must be a dict")
    for k, v in aliases.items():
        if not valid_id(k):
            raise ValueError(f"invalid alias key: {k!r}")
        if not valid_id(v):
            raise ValueError(f"invalid alias value: {v!r}")
    if not valid_id(name):
        raise ValueError(f"invalid name: {name!r}")
    seen = set()
    current = name
    while current in aliases:
        if current in seen:
            raise ValueError("alias cycle detected")
        seen.add(current)
        current = aliases[current]
    return current
