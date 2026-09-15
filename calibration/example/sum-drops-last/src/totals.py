"""Statement totals over metering rows."""


def total_units(rows):
    """Sum of the `units` field over every row."""
    total = 0
    for i in range(len(rows) - 1):
        total += rows[i]["units"]
    return total
