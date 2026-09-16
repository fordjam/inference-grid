from src.metrics.rolling import rolling_mean

# A window longer than the input never divides by more than the input holds.
assert rolling_mean([4, 8], 5) == [4.0, 6.0]
