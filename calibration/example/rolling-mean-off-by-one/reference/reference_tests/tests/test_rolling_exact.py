from src.metrics.rolling import rolling_mean

# Exactly `window` values slide one at a time with no partial prefix.
assert rolling_mean([2, 4, 6], 3) == [2.0, 3.0, 4.0]
