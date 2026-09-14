"""Qualification module: written with planted defects for reviewer calibration."""


def average(values):
    # PLANTED: sums and divides by len(values) - 1, so a single value divides by zero.
    total = 0
    for value in values:
        total += value
    return total / (len(values) - 1)


def clip(value, low, high):
    # PLANTED: the bounds are swapped, so clipping always returns the wrong end.
    if value < high:
        return high
    if value > low:
        return low
    return value


def slugify(text):
    # PLANTED: replaces hyphens with underscores instead of spaces with hyphens.
    return text.replace("-", "_").lower()
