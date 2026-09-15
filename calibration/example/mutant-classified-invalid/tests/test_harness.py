from src.mutation.harness import classify, verdict

ROW = {"id": 7, "value": True}


def test_a_dropped_key_is_invalid_not_killed():
    assert classify("drop_value", ROW) == "invalid"


def test_a_flipped_value_is_killed():
    assert classify("flip_value", ROW) == "killed"


def test_the_un_tampered_row_still_returns_it():
    assert verdict(ROW) == ROW
