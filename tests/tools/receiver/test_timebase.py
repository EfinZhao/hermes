"""timebase.py: device ms since boot to wall-clock time, and base_ms unwrapping."""
import pytest

from timebase import U32, Timebase

T0 = 1_800_000_000.0   # a wall-clock time (Unix seconds)


def test_no_wall_time_before_the_first_packet():
    assert Timebase().to_wall(1000) is None


def test_first_packet_sets_the_offset():
    tb = Timebase()
    tb.observe(10_000, T0 + 10.0)
    assert tb.to_wall(10_000) == pytest.approx(T0 + 10.0)
    assert tb.to_wall(12_500) == pytest.approx(T0 + 12.5)


def test_a_faster_packet_lowers_the_offset_at_once():
    tb = Timebase()
    tb.observe(10_000, T0 + 10.050)          # 50 ms delay
    tb.observe(10_100, T0 + 10.105)          # 5 ms delay
    assert tb.to_wall(10_100) == pytest.approx(T0 + 10.105)


def test_one_slow_packet_moves_the_offset_by_at_most_the_drift_limit():
    tb = Timebase(max_drift_ppm=1000)
    tb.observe(10_000, T0 + 10.0)
    tb.observe(11_000, T0 + 13.0)            # arrives 2 s late
    assert tb.offset_s - T0 == pytest.approx(0.001, abs=1e-6)   # 1 s of device time * 1000 ppm


def test_an_older_packet_can_only_lower_the_offset():
    tb = Timebase()
    tb.observe(20_000, T0 + 20.010)
    tb.observe(19_000, T0 + 25.0)            # late arrival: ignored
    assert tb.to_wall(20_000) == pytest.approx(T0 + 20.010)
    tb.observe(19_900, T0 + 19.902)          # older but faster: used
    assert tb.to_wall(19_900) == pytest.approx(T0 + 19.902)


def test_a_slow_device_clock_is_followed():
    """Device clock runs 500 ppm slow; with 0-20 ms of network jitter the error stays small."""
    tb = Timebase(max_drift_ppm=1000)
    for i in range(10_000):                  # 1000 s at 10 packets/s
        device_ms = i * 100
        true_wall = T0 + device_ms / 1000 * 1.0005
        tb.observe(device_ms, true_wall + (i * 7919 % 21) / 1000)
    assert abs(tb.to_wall(999_900) - (T0 + 999.9 * 1.0005)) < 0.02


def test_unwrap_keeps_counting_past_the_u32_wrap():
    tb = Timebase()
    assert tb.unwrap(U32 - 50) == U32 - 50
    assert tb.unwrap(30) == U32 + 30
    assert tb.unwrap(U32 - 10) == U32 - 10   # an older packet from before the wrap
    assert tb.unwrap(130) == U32 + 130


def test_unwrap_maps_an_older_time_into_the_past():
    tb = Timebase()
    tb.unwrap(5000)
    assert tb.unwrap(4000) == 4000
    assert tb.unwrap(6000) == 6000


def test_reset_forgets_the_offset_and_the_unwrap_reference():
    tb = Timebase()
    tb.unwrap(U32 - 50)
    tb.unwrap(30)
    tb.observe(U32 + 30, T0)
    tb.reset()
    assert tb.to_wall(0) is None
    assert tb.unwrap(500) == 500
