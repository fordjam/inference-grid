from src.metrics.rolling import rolling_mean

# A window of one returns each value as a float.
assert rolling_mean([7, 9, 4], 1) == [7.0, 9.0, 4.0]
