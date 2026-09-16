from src.metrics.rolling import rolling_mean

# Once the prefix is longer than the window, the trailing window slides: the oldest
# value must leave before the mean is taken.
assert rolling_mean([1, 2, 3, 4], 2) == [1.0, 1.5, 2.5, 3.5]
