import re

import pytest

from src.cloid import BUY, SELL, STOP, OrderTag, decode, encode, fingerprint

FP = fingerprint("BTC", 75000, 83000, 20, 3)


def test_roundtrip_buy():
    tag = decode(encode(FP, 7, BUY))
    assert tag == OrderTag(fingerprint=FP, cell=7, side=BUY)


def test_roundtrip_sell_on_the_highest_cell():
    tag = decode(encode(FP, 65535, SELL))
    assert tag == OrderTag(fingerprint=FP, cell=65535, side=SELL)


def test_cloid_is_what_hyperliquid_accepts():
    # The SDK's Cloid wants "0x" plus exactly 32 hex characters (16 bytes).
    assert re.fullmatch(r"0x[0-9a-f]{32}", encode(FP, 0, BUY))


def test_every_cloid_is_unique():
    # Two orders on the same cell over time must never share a cloid.
    assert len({encode(FP, 3, BUY) for _ in range(200)}) == 200


def test_fingerprint_is_stable_across_int_and_float_bounds():
    # YAML gives 83000 or 83000.0 depending on how it was typed; that must not
    # turn a running grid into an "old" one.
    assert fingerprint("BTC", 75000, 83000, 20, 3) == fingerprint("BTC", 75000.0, 83000.0, 20, 3.0)


@pytest.mark.parametrize("changed", [
    ("ETH", 75000, 83000, 20, 3),
    ("BTC", 74000, 83000, 20, 3),
    ("BTC", 75000, 84000, 20, 3),
    ("BTC", 75000, 83000, 21, 3),
    ("BTC", 75000, 83000, 20, 5),
])
def test_fingerprint_changes_with_every_setting(changed):
    assert fingerprint(*changed) != FP


def test_fingerprint_is_six_bytes():
    assert len(FP) == 6


@pytest.mark.parametrize("cloid", [
    None,
    "",
    "0x",
    "not-hex",
    "0x" + "00" * 16,                 # no magic: someone else's order
    "0x" + "ab" * 16,
    "0x6773" + "00" * 14,             # magic, but version 0
    encode(FP, 1, BUY)[:-2],          # too short
])
def test_decode_rejects_what_is_not_ours(cloid):
    assert decode(cloid) is None


def test_decode_rejects_an_unknown_side_byte():
    good = encode(FP, 1, BUY)
    raw = bytearray.fromhex(good[2:])
    raw[11] = 0x7F
    assert decode("0x" + raw.hex()) is None


def test_decode_accepts_uppercase_hex():
    assert decode(encode(FP, 2, SELL).upper().replace("0X", "0x")) is not None


def test_encode_refuses_a_cell_out_of_range():
    with pytest.raises(ValueError):
        encode(FP, 65536, BUY)
    with pytest.raises(ValueError):
        encode(FP, -1, BUY)


def test_encode_refuses_an_unknown_side():
    with pytest.raises(ValueError):
        encode(FP, 1, "LONG")


def test_roundtrip_stop():
    assert decode(encode(FP, 0, STOP)) == OrderTag(fingerprint=FP, cell=0, side=STOP)
