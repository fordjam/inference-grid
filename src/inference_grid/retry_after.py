import email.utils
import math
from fractions import Fraction


def _finite_num(x):
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        raise ValueError("not finite numeric")
    try:
        f = float(x)
    except OverflowError as exc:
        raise ValueError("numeric overflow") from exc
    if f != f or f in (float("inf"), float("-inf")):
        raise ValueError("not finite numeric")
    return f


def retry_deadline(value, now, fallback_seconds=60):
    now = _finite_num(now)
    fallback = _finite_num(fallback_seconds)
    if fallback <= 0:
        raise ValueError("fallback must be positive")
    delay = fallback
    if isinstance(value, str):
        s = value.strip()
        if s and s.isdigit() and s.isascii():
            try:
                delay = int(s)
            except (ValueError, OverflowError) as exc:
                raise ValueError("delay overflow") from exc
        else:
            try:
                dt = email.utils.parsedate_to_datetime(s)
            except (TypeError, ValueError, IndexError):
                dt = None
            if dt is not None and dt.tzinfo is not None:
                # Preserve fractional receive time; date is already an absolute deadline.
                return max(now, _finite_num(dt.timestamp()))
            else:
                delay = fallback
    try:
        deadline = now + delay
    except OverflowError:
        raise ValueError("deadline overflow")
    if deadline != deadline or deadline in (float("inf"), float("-inf")):
        raise ValueError("nonfinite deadline")
    # Never round a positive wait down when representing the absolute deadline.
    if Fraction(deadline) < Fraction(now) + Fraction(delay):
        deadline = math.nextafter(deadline, math.inf)
        if not math.isfinite(deadline):
            raise ValueError("deadline overflow")
    return max(now, deadline)


def cooldown_key(account, endpoint):
    if not (isinstance(account, str) and isinstance(endpoint, str)) or not account or not endpoint:
        raise ValueError("empty identifier")
    return (account, endpoint)
