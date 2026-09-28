"""Convert legacy (old-bot) file_ids to Bot-API-usable file_ids.

The old bot stored a custom encoding in MongoDB::

    custom_b64(RLE(struct.pack("<iiqq", file_type, dc_id, media_id,
                              access_hash) + b"\\x16\\x04"))

i.e. the raw MTProto identifiers with the old bot's own 2-byte version tail.
The new bot speaks Bot API, whose file_id is Telegram's standard persistent
file-id serialization (RLE + urlsafe-base64, no padding).

Bot API wire format (little-endian), verified byte-for-byte against the
official Bot API server (see mtgo-labs/mtgo-bot-api, internal/fileid/,
"verified end-to-end against live api.telegram.org")::

    document: type(u32, |1<<25 if file_reference) | dc(u32) | [fileref] |
              id(i64) | access_hash(i64) | subVersion(u8=60) | version(u8=4)

``file_reference`` is encoded TL-style (1 length byte if < 254, else
254 + 3 length bytes, padded to a 4-byte boundary). We do not have a
file_reference in the legacy data, so we encode without one (no flag bit);
(dc_id, media_id, access_hash) alone is sufficient for the Bot API to
locate the file.

How this differs from pyrogram's MTProto FileId format (which the previous
version of this module used, and which the Bot API rejects with
"Wrong remote file identifier specified: can't unserialize it"):

* pyrogram:  [type i32][dc i32][id i64][access_hash i64]
             [minor i32][major i32][minor u8][major u8]  (version 4.30,
             duplicated minor/major as both i32 and u8)
* Bot API:   [type u32][dc u32][id i64][access_hash i64]
             [subVersion u8=60][version u8=4]  (single trailing bytes,
             subVersion 60 -- NOT pyrogram's 30)
* Bot API sets bit 1<<25 in the type field when a file_reference is
  present, and bit 1<<24 for web locations; pyrogram has no such flags
  in this position.
* RLE: zero runs are encoded as 0x00 <count> with count capped at 250
  per run (pyrogram caps differently); base64 is urlsafe without padding.

This module uses only the stdlib, so the bot needs no MTProto dependency.
"""
from __future__ import annotations

import base64
import struct

# Old bot's custom 2-byte version tail appended before RLE/base64.
_OLD_TAIL = b"\x16\x04"

# Bot API persistent file-id framing for document-type files.
_SUB_VERSION = 60
_VERSION = 4
_FILE_REFERENCE_FLAG = 1 << 25

# RLE zero-run cap used by the official encoding.
_RLE_RUN_CAP = 250


def _b64_decode(s: str) -> bytes:
    s = s.strip()
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _b64_encode(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _rle_decode(data: bytes) -> bytes:
    out = bytearray()
    i = 0
    n = len(data)
    while i < n:
        if data[i] == 0:
            out += b"\x00" * data[i + 1]
            i += 2
        else:
            out.append(data[i])
            i += 1
    return bytes(out)


def _rle_encode(data: bytes) -> bytes:
    out = bytearray()
    i = 0
    n = len(data)
    while i < n:
        if data[i] == 0:
            j = i
            while j < n and data[j] == 0 and (j - i) < _RLE_RUN_CAP:
                j += 1
            out += b"\x00" + bytes([j - i])
            i = j
        else:
            out.append(data[i])
            i += 1
    return bytes(out)


def _pack_file_reference(buf: bytearray, ref: bytes) -> None:
    """Append a TL-style length-prefixed byte string, padded to 4 bytes."""
    if len(ref) < 254:
        buf.append(len(ref))
    else:
        buf.append(254)
        buf += struct.pack("<I", len(ref))[:3]
    buf += ref
    while len(buf) % 4 != 0:
        buf.append(0)


def decode_legacy(old_file_id: str) -> tuple[int, int, int, int]:
    """Return ``(file_type, dc_id, media_id, access_hash)`` from a legacy id."""
    raw = _rle_decode(_b64_decode(old_file_id))
    if len(raw) < 26 or raw[-2:] != _OLD_TAIL:
        raise ValueError("not a legacy old-bot file_id")
    core = raw[:-2]
    if len(core) != 24:
        raise ValueError("unexpected legacy payload length")
    return struct.unpack("<iiqq", core)


def decode_bot_api(file_id: str) -> dict:
    """Decode a Bot API file_id into its fields (for verification).

    Returns a dict with type, dc_id, media_id, access_hash, file_reference,
    sub_version, version. Raises ValueError on malformed input.
    """
    raw = _rle_decode(_b64_decode(file_id))
    if len(raw) < 26:
        raise ValueError("file_id too short")
    if raw[-1] != _VERSION:
        raise ValueError(f"unsupported version byte: {raw[-1]}")
    sub_version = raw[-2]
    body = raw[:-2]
    type_id = struct.unpack("<I", body[0:4])[0]
    dc_id = struct.unpack("<I", body[4:8])[0]
    file_type = type_id & ~((1 << 25) | (1 << 24))
    off = 8
    file_reference = b""
    if type_id & _FILE_REFERENCE_FLAG:
        first = body[off]
        off += 1
        if first == 254:
            size = struct.unpack("<I", body[off:off + 3] + b"\x00")[0]
            off += 3
        else:
            size = first
        file_reference = body[off:off + size]
        if len(file_reference) != size:
            raise ValueError("truncated file_reference")
        off += size
        while off % 4 != 0:
            off += 1
    if len(body) < off + 16:
        raise ValueError("truncated id/access_hash")
    media_id, access_hash = struct.unpack("<qq", body[off:off + 16])
    return {
        "type": file_type,
        "dc_id": dc_id,
        "media_id": media_id,
        "access_hash": access_hash,
        "file_reference": file_reference,
        "sub_version": sub_version,
        "version": _VERSION,
    }


def to_bot_api(old_file_id: str) -> str:
    """Convert a legacy old-bot file_id to a Bot-API-usable file_id string.

    Layout: [type u32][dc u32][id i64][access_hash i64][60][4],
    then RLE + urlsafe-base64 (no padding). No file_reference is embedded
    (legacy data has none); the Bot API resolves the file from
    (dc_id, media_id, access_hash).
    """
    file_type, dc_id, media_id, access_hash = decode_legacy(old_file_id)
    buf = bytearray()
    buf += struct.pack("<I", file_type & 0xFFFFFFFF)
    buf += struct.pack("<I", dc_id & 0xFFFFFFFF)
    # No file_reference available from legacy data: no flag, no field.
    buf += struct.pack("<q", media_id)
    buf += struct.pack("<q", access_hash)
    buf += bytes([_SUB_VERSION, _VERSION])
    return _b64_encode(_rle_encode(bytes(buf)))


def pack_bot_file_id_typed(file_type: int, dc_id: int, media_id: int,
                           access_hash: int,
                           file_reference: bytes | None) -> str | None:
    """Pack a FRESH MTProto file into a Bot API file_id string.

    Unlike telethon's ``pack_bot_file_id`` (which omits the file_reference
    and which the Bot API rejects), this embeds the file_reference fetched
    with the file, using the verified layout::

        [type u32 | 1<<25][dc u32][fileref TL][id i64][access_hash i64][60][4]

    Returns None when there is no file_reference -- without it the Bot API
    cannot use the id ("can't unserialize it").
    """
    if not file_reference:
        return None
    buf = bytearray()
    buf += struct.pack("<I", (file_type | _FILE_REFERENCE_FLAG) & 0xFFFFFFFF)
    buf += struct.pack("<I", dc_id & 0xFFFFFFFF)
    _pack_file_reference(buf, bytes(file_reference))
    buf += struct.pack("<q", media_id)
    buf += struct.pack("<q", access_hash)
    buf += bytes([_SUB_VERSION, _VERSION])
    return _b64_encode(_rle_encode(bytes(buf)))


if __name__ == "__main__":
    # Self-test: legacy sample -> Bot API id -> decode and verify fields.
    sample = "BQADBAADbxIAAiI9kVE8STv1CqNIQhYE"
    ftype, dc, mid, ah = decode_legacy(sample)
    print("legacy:", ftype, dc, mid, ah)
    new_id = to_bot_api(sample)
    print("bot_api:", new_id)
    d = decode_bot_api(new_id)
    print("decoded:", d)
    assert d["type"] == ftype == 5
    assert d["dc_id"] == dc == 4
    assert d["media_id"] == mid
    assert d["access_hash"] == ah
    assert d["sub_version"] == 60 and d["version"] == 4
    assert d["file_reference"] == b""
    # Round-trip: decode(encode(x)) fields must match.
    print("OK: conversion round-trips through the Bot API decoder")

    # Fresh pack: with a file_reference the id must decode back completely.
    fake_ref = bytes(range(1, 32))
    packed = pack_bot_file_id_typed(5, 4, 123456789, 987654321, fake_ref)
    assert packed, "pack returned None"
    d2 = decode_bot_api(packed)
    assert d2["type"] == 5 and d2["dc_id"] == 4
    assert d2["media_id"] == 123456789 and d2["access_hash"] == 987654321
    assert d2["file_reference"] == fake_ref
    assert d2["sub_version"] == 60 and d2["version"] == 4
    assert pack_bot_file_id_typed(5, 4, 1, 1, None) is None
    assert pack_bot_file_id_typed(5, 4, 1, 1, b"") is None
    print("OK: fresh pack with file_reference round-trips")
