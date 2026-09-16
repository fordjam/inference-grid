"""Rolling aggregates over a stream of samples for the metrics dashboard."""


def rolling_mean(values, window):
    """The mean of each trailing `window` of values, one per position, in order.

    A position whose prefix is shorter than `window` uses what exists before it, so the
    first `window - 1` means are partial. An empty input has no positions, and a window
    of one returns each value as a float.
    """
    if window < 1:
        raise ValueError("window must be at least 1")
    means = []
    total = 0
    for index, value in enumerate(values):
        total += value
        if index > window:
            total -= values[index - window]
        count = min(index + 1, window)
        means.append(total / count)
    return means


def mean(values):
    """The arithmetic mean of values; an empty input is 0."""
    if not values:
        return 0
    return sum(values) / len(values)
