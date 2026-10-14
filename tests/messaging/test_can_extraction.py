"""CAN signal extraction: little-endian (Intel), multiplexed Intel messages, short frames."""
import random

import cantools
import pytest
from cantools.database.can import Message, Signal
from cantools.database.conversion import BaseConversion

import msgcodec as m


def signal(name, start, length, *, signed=False, scale=1.0, offset=0.0, order="little_endian", **kw):
    return Signal(name, start=start, length=length, byte_order=order, is_signed=signed,
                  conversion=BaseConversion.factory(scale=scale, offset=offset), **kw)


def random_frames(n, seed, size=8):
    rng = random.Random(seed)
    return [bytes(rng.randrange(256) for _ in range(size)) for _ in range(n)]


# ---------------------------------------------------------------- little-endian (Intel)
INTEL_SIGNALS = [                                   # non-overlapping, some crossing byte boundaries
    signal("byte_aligned", 0, 8),
    signal("crosses_bytes", 12, 12),
    signal("signed_odd", 25, 11, signed=True, scale=0.5, offset=-3.0),
    signal("wide", 36, 16, scale=0.01),
    signal("signed_wide", 52, 11, signed=True),
    signal("single_bit", 63, 1),
]


@pytest.mark.parametrize("sig", INTEL_SIGNALS, ids=lambda s: s.name)
def test_little_endian_extraction_matches_cantools(sig):
    msg = Message(0x100, "Intel", 8, INTEL_SIGNALS, strict=False)
    for data in random_frames(300, seed=sig.start):
        want = msg.decode(data, decode_choices=False, scaling=True)[sig.name]
        got = m.extract_signal(data, sig.start, sig.length, "little_endian", sig.is_signed,
                               sig.scale, sig.offset)
        assert abs(want - got) < 1e-9, (data.hex(), want, got)


def test_little_endian_known_value():
    # 0x1234 stored low byte first: bytes 34 12
    assert m.extract_signal(bytes([0x34, 0x12, 0, 0, 0, 0, 0, 0]), 0, 16, "little_endian", False, 1, 0) == 0x1234


# ---------------------------------------------------------------- multiplexed Intel message
def mux_message():
    sigs = [signal("sel", 0, 4, is_multiplexer=True)]
    for k in range(3):
        sigs.append(signal(f"x{k}", 8, 16, signed=bool(k % 2), scale=0.25, offset=k,
                           multiplexer_ids=[k], multiplexer_signal="sel"))
    return Message(0x200, "Muxed", 8, sigs, strict=False)


def mux_channel(msg, sig):
    sel = next(s for s in msg.signals if s.is_multiplexer)
    return {"src": {"start_bit": sig.start, "length": sig.length, "byte_order": sig.byte_order,
                    "signed": sig.is_signed, "scale": sig.scale, "offset": sig.offset,
                    "mux": {"start_bit": sel.start, "length": sel.length, "value": sig.multiplexer_ids[0]}}}


def test_intel_multiplexed_channels_match_cantools():
    """The selector must be read with the signal's byte order, not always big-endian."""
    msg = mux_message()
    seen_none = seen_value = 0
    for data in random_frames(400, seed=7):
        data = bytes([(data[0] & 0xF0) | (data[1] % 3)]) + data[1:]        # selector 0..2 (the defined values)
        decoded = msg.decode(data, decode_choices=False, scaling=True)
        for sig in (s for s in msg.signals if not s.is_multiplexer):
            got = m.extract_channel(data, mux_channel(msg, sig))
            want = decoded.get(sig.name)
            if want is None:
                assert got is None
                seen_none += 1
            else:
                assert abs(want - got) < 1e-9
                seen_value += 1
    assert seen_none and seen_value


# ---------------------------------------------------------------- short frames
CHANNEL = {"src": {"start_bit": 23, "length": 16, "byte_order": "big_endian", "signed": False,
                   "scale": 1.0, "offset": 0.0}}


@pytest.mark.parametrize("order, start, length, size", [
    ("big_endian", 23, 16, 2),          # needs bytes 2-3
    ("big_endian", 7, 16, 1),
    ("little_endian", 24, 16, 4),
    ("little_endian", 0, 8, 0),
])
def test_extract_signal_raises_a_clear_error_on_a_short_frame(order, start, length, size):
    with pytest.raises(m.FrameTooShort, match="signal needs"):
        m.extract_signal(bytes(size), start, length, order, False, 1, 0)


def test_extract_channel_returns_none_on_a_short_frame():
    assert m.extract_channel(bytes(2), CHANNEL) is None
    assert m.extract_channel(bytes(4), CHANNEL) == 0.0           # long enough: decodes


def test_extract_channel_returns_none_when_only_the_selector_is_cut_off():
    ch = {"src": {**CHANNEL["src"], "start_bit": 7, "mux": {"start_bit": 40, "length": 4, "value": 1}}}
    assert m.extract_channel(bytes(2), ch) is None


def test_frame_too_short_is_a_value_error():
    assert issubclass(m.FrameTooShort, ValueError)
