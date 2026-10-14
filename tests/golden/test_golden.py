"""The golden files in messaging/golden/: in sync with the generator, self-consistent, and complete."""
import json
import struct

import pytest

import make_golden
import msgcodec as m
from conftest import MESSAGING, crc32_reference

GOLDEN = MESSAGING / "golden"
MSG_TYPES = {"DATA": m.DATA, "CONFIG": m.CONFIG, "ACK": m.ACK, "RAW_CAN": m.RAW_CAN}
PACKET_FILES = ["data", "raw_can", "config", "ack"]


def load(name):
    return json.loads((GOLDEN / f"{name}.json").read_text())


def cases(name):
    return [pytest.param(c, id=c["name"]) for c in load(name)["cases"]]


def packet_cases():
    return [pytest.param(c, id=f"{f}:{c['name']}") for f in PACKET_FILES for c in load(f)["cases"]]


def test_committed_golden_files_are_up_to_date():
    """Fails if msgcodec.py or make_golden.py changed without regenerating (uv run python tools/make_golden.py)."""
    assert make_golden.main(["--check"]) == 0


# ---------------------------------------------------------------- valid packets
@pytest.mark.parametrize("case", packet_cases())
def test_every_packet_decodes_to_its_listed_header_and_records(case):
    for pkt in case["packets"]:
        buf = bytes.fromhex(pkt["hex"])
        assert len(buf) == pkt["size"]
        # parse the header with a layout written out here, not taken from msgcodec
        magic, version, msg_type, device, flags, reg_ver, seq, base_ms, count, payload_len = \
            struct.unpack_from("<HBBBBHIIHH", buf)
        assert (magic, version) == (0x4D48, 1)
        assert {"msg_type": msg_type, "device_id": device, "flags": flags, "registry_version": reg_ver, "seq": seq,
                "base_ms": base_ms, "record_count": count, "payload_len": payload_len} == pkt["header"]
        assert len(buf) == 20 + payload_len + 4
        decoded = m.decode_packet(buf)
        assert [make_golden.record_json(decoded.msg_type, r) for r in decoded.records] == pkt["records"]


@pytest.mark.parametrize("case", packet_cases())
def test_every_packet_ends_with_the_crc_of_the_rest(case):
    for pkt in case["packets"]:
        buf = bytes.fromhex(pkt["hex"])
        crc = int.from_bytes(buf[-4:], "little")
        assert crc == crc32_reference(buf[:-4])
        assert pkt["crc32"] == f"0x{crc:08x}"


@pytest.mark.parametrize("case", [c for f in PACKET_FILES for c in cases(f) if c.values[0]["kind"] == "encode"])
def test_encode_cases_rebuild_the_same_bytes(case):
    msg_type = MSG_TYPES[case["msg_type"]]
    p = case["params"]
    packed = [m.RECORD[msg_type].pack(*make_golden.record_from_json(msg_type, r)) for r in case["records"]]
    buf = m.encode_packet(msg_type, packed, device_id=p["device_id"], registry_version=p["registry_version"],
                          seq=p["seq"], base_ms=p["base_ms"], flags=p["flags"])
    assert buf.hex() == case["packets"][0]["hex"]


@pytest.mark.parametrize("case", [c for f in PACKET_FILES for c in cases(f) if c.values[0]["kind"] == "packetize"])
def test_packetize_cases_rebuild_the_same_packets(case):
    msg_type, p = MSG_TYPES[case["msg_type"]], case["params"]
    pk = m.Packetizer(p["device_id"], p["registry_version"], p["max_packet_bytes"])
    pk.seq = p["start_seq"]
    if msg_type == m.DATA:
        items = [(i["t_ms"], i["channel_id"], make_golden.bits_f32(int(i["value_bits"], 16)), i["quality"])
                 for i in case["items"]]
    else:
        items = [(i["t_ms"], int(i["can_id"], 16), i["bus"], i["dlc"], bytes.fromhex(i["data"]))
                 for i in case["items"]]
    bufs = pk.build(msg_type, items, flags=p["flags"])
    assert [b.hex() for b in bufs] == [x["hex"] for x in case["packets"]]
    assert all(len(b) <= p["max_packet_bytes"] for b in bufs)
    seqs = [x["header"]["seq"] for x in case["packets"]]
    assert seqs == [(p["start_seq"] + i) & 0xFFFFFFFF for i in range(len(seqs))]


# ---------------------------------------------------------------- CRC vectors and invalid packets
@pytest.mark.parametrize("case", cases("crc32"))
def test_crc_vectors_match_an_independent_implementation(case):
    data = bytes.fromhex(case["input_hex"])
    assert case["crc32"] == f"0x{crc32_reference(data):08x}"


@pytest.mark.parametrize("case", cases("invalid"))
def test_invalid_packets_are_rejected_for_the_listed_reason(case):
    with pytest.raises(m.PacketError, match=case["python_message"]):
        m.decode_packet(bytes.fromhex(case["hex"]))


# ---------------------------------------------------------------- what the files must contain (issue #4)
def names(file):
    return {c["name"] for c in load(file)["cases"]}


def test_there_are_golden_cases_for_every_message_type():
    for f in PACKET_FILES:
        assert load(f)["cases"], f
    assert {c["msg_type"] for f in PACKET_FILES for c in load(f)["cases"]} == set(MSG_TYPES)


def test_the_crc_check_value_is_included():
    vectors = {c["input_hex"]: c["crc32"] for c in load("crc32")["cases"]}
    assert vectors[b"123456789".hex()] == "0xcbf43926"


def test_a_multi_packet_split_is_included():
    case = next(c for c in load("data")["cases"] if c["name"] == "split_into_three_packets")
    assert [p["size"] for p in case["packets"]] == [1194, 1194, 384]
    assert [p["header"]["record_count"] for p in case["packets"]] == [130, 130, 40]


def test_a_sequence_wrap_is_included():
    case = next(c for c in load("data")["cases"] if c["name"] == "seq_wraps_at_32_bits")
    assert [p["header"]["seq"] for p in case["packets"]] == [0xFFFFFFFE, 0xFFFFFFFF, 0]


def test_the_spec_example_is_the_first_data_case():
    case = load("data")["cases"][0]
    assert case["packets"][0]["hex"] == ("484d01001400010000000000e8030000"
                                         "0100090064000000" "0000fa4500" "f6196676")


def test_invalid_cases_cover_each_error_reason():
    assert {c["error"] for c in load("invalid")["cases"]} == {
        "too_short", "bad_magic", "unsupported_format_version", "length_mismatch", "crc_mismatch",
        "record_count_mismatch"}
