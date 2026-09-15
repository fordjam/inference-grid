from metering.totals import total_units


def test_total_units_of_three_rows():
    rows = [{"units": 2}, {"units": 3}, {"units": 5}]
    assert total_units(rows) == 5 + 5
