"""The client order id (cloid) carries which grid cell an order belongs to.

The exchange keeps every resting order and hands the cloid back with it, so the
bot can tell after a restart which line each order is on without keeping any
record of its own. Prices are never used for that: the exchange rounds them and
fills at prices of its own.

Layout, 16 bytes:

    0-1    magic 0x6773 ("gs"): this is a gridstatic order
    2      version
    3-8    grid fingerprint: coin, bounds, line count and leverage
    9-10   cell index (uint16)
    11     side
    12-15  nonce, so no two orders ever share a cloid
"""

import hashlib
import random
from dataclasses import dataclass

BUY = "BUY"
SELL = "SELL"

_MAGIC = b"\x67\x73"
_VERSION = 0x01
_SIDE_BYTE = {BUY: 0x01, SELL: 0x02}
_BYTE_SIDE = {v: k for k, v in _SIDE_BYTE.items()}


@dataclass(frozen=True)
class OrderTag:
    fingerprint: bytes
    cell: int
    side: str


def fingerprint(coin: str, lower: float, upper: float,
                num_lines: int, leverage: float) -> bytes:
    """Six bytes that change whenever the grid's lines would change. Numbers go
    through float() first: YAML reads 83000 and 83000.0 as different types, and
    retyping a bound must not make a running grid look like an old one."""
    key = f"{coin}|{float(lower)!r}|{float(upper)!r}|{int(num_lines)}|{float(leverage)!r}"
    return hashlib.sha256(key.encode()).digest()[:6]


def encode(fp: bytes, cell: int, side: str, nonce: int | None = None) -> str:
    if len(fp) != 6:
        raise ValueError(f"fingerprint must be 6 bytes, got {len(fp)}")
    if not 0 <= cell <= 0xFFFF:
        raise ValueError(f"cell out of range: {cell}")
    if side not in _SIDE_BYTE:
        raise ValueError(f"unknown side: {side}")
    if nonce is None:
        nonce = random.getrandbits(32)
    raw = (_MAGIC + bytes([_VERSION]) + fp + cell.to_bytes(2, "big")
           + bytes([_SIDE_BYTE[side]]) + nonce.to_bytes(4, "big"))
    return "0x" + raw.hex()


def decode(cloid: str | None) -> OrderTag | None:
    """The tag of one of our orders, or None for anything else -- no cloid,
    someone else's cloid, or one this version does not understand."""
    if not isinstance(cloid, str) or len(cloid) != 34 or cloid[:2].lower() != "0x":
        return None
    try:
        raw = bytes.fromhex(cloid[2:])
    except ValueError:
        return None
    if raw[:2] != _MAGIC or raw[2] != _VERSION or raw[11] not in _BYTE_SIDE:
        return None
    return OrderTag(fingerprint=raw[3:9],
                    cell=int.from_bytes(raw[9:11], "big"),
                    side=_BYTE_SIDE[raw[11]])
