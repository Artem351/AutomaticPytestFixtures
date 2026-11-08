import pytest


@pytest.mark.parametrize("expected", [True, False], ids=["main", "aux"])
def test_power_state(power, expected):
    assert power.is_on() is expected
