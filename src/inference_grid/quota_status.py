def classify_quota_status(status):
    if isinstance(status, bool) or not isinstance(status, int):
        raise ValueError("status must be an integer")
    if not 100 <= status <= 599:
        raise ValueError("status must be a valid HTTP status code (100..599)")
    if status == 200:
        return "validate"
    if status == 429:
        return "cooldown"
    if status in (401, 403):
        return "auth_required"
    if 500 <= status <= 599:
        return "transient_error"
    return "http_error"
