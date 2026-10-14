"""apply_quality: clamp to the registry range and flag it."""
import math

import pytest

import msgcodec as m

CH = {"min": -40, "max": 215}


@pytest.mark.parametrize("value, expected", [
    (90.0, (90.0, 0)),                              # inside the range
    (-40.0, (-40.0, 0)),                            # exactly on a limit is not clamped
    (215.0, (215.0, 0)),
    (215.5, (215.0, m.Q_CLAMPED)),                  # above
    (9000.0, (215.0, m.Q_CLAMPED)),
    (-40.5, (-40.0, m.Q_CLAMPED)),                  # below
    (-1e9, (-40.0, m.Q_CLAMPED)),
    (math.inf, (215.0, m.Q_CLAMPED)),
    (-math.inf, (-40.0, m.Q_CLAMPED)),
])
def test_clamping(value, expected):
    assert m.apply_quality(value, CH) == expected


def test_clamped_limit_is_returned_as_a_float():
    value, _ = m.apply_quality(500, {"min": 0, "max": 100})
    assert value == 100.0 and isinstance(value, float)


def test_nan_is_kept_but_flagged_invalid():
    value, quality = m.apply_quality(math.nan, CH)
    assert math.isnan(value) and quality == m.Q_INVALID
