"""
LZ4 frame decompression.

Uses the ``lz4`` package when it is importable (Amethyst vendors it) and
falls back to a small pure-Python decoder otherwise.  SWF files are small, so
the fallback is fast enough.
"""

from __future__ import annotations

import struct

try:  # pragma: no cover - depends on environment
    import lz4.frame as _lz4frame  # type: ignore
except Exception:  # noqa: BLE001
    _lz4frame = None

MAGIC = 0x184D2204
_SKIPPABLE_MASK = 0xFFFFFFF0
_SKIPPABLE_MAGIC = 0x184D2A50


class LZ4Error(ValueError):
    pass


def _block(src: bytes | memoryview, out: bytearray) -> None:
    """Decode one LZ4 block, appending to ``out`` (which may hold prior blocks)."""
    i, n = 0, len(src)
    while i < n:
        token = src[i]
        i += 1
        lit = token >> 4
        if lit == 15:
            while True:
                b = src[i]
                i += 1
                lit += b
                if b != 255:
                    break
        out += src[i:i + lit]
        i += lit
        if i >= n:  # last sequence carries literals only
            break
        offset = src[i] | (src[i + 1] << 8)
        i += 2
        if offset == 0 or offset > len(out):
            raise LZ4Error("invalid LZ4 match offset")
        mlen = token & 15
        if mlen == 15:
            while True:
                b = src[i]
                i += 1
                mlen += b
                if b != 255:
                    break
        mlen += 4
        start = len(out) - offset
        if mlen <= offset:
            out += out[start:start + mlen]
        else:  # overlapping copy repeats the last ``offset`` bytes
            chunk = bytes(out[start:])
            reps, rest = divmod(mlen, offset)
            out += chunk * reps + chunk[:rest]


def _decompress_py(data: bytes) -> bytes:
    view = memoryview(data)
    pos = 0
    out = bytearray()
    while pos + 4 <= len(view):
        magic = struct.unpack_from("<I", view, pos)[0]
        pos += 4
        if (magic & _SKIPPABLE_MASK) == _SKIPPABLE_MAGIC:
            size = struct.unpack_from("<I", view, pos)[0]
            pos += 4 + size
            continue
        if magic != MAGIC:
            raise LZ4Error("not an LZ4 frame")
        flg = view[pos]
        pos += 2  # FLG + BD
        if (flg >> 6) != 1:
            raise LZ4Error("unsupported LZ4 frame version")
        block_checksum = bool(flg & 0x10)
        content_checksum = bool(flg & 0x04)
        if flg & 0x08:
            pos += 8  # content size
        if flg & 0x01:
            pos += 4  # dictionary id
        pos += 1  # header checksum
        frame_out = bytearray()
        while True:
            bsize = struct.unpack_from("<I", view, pos)[0]
            pos += 4
            if bsize == 0:
                break
            raw = bool(bsize & 0x80000000)
            bsize &= 0x7FFFFFFF
            blk = view[pos:pos + bsize]
            pos += bsize
            if raw:
                frame_out += blk
            else:
                _block(blk, frame_out)
            if block_checksum:
                pos += 4
        if content_checksum:
            pos += 4
        out += frame_out
    return bytes(out)


def decompress(data: bytes) -> bytes:
    if _lz4frame is not None:
        return _lz4frame.decompress(data)
    return _decompress_py(data)


def compress(data: bytes) -> bytes | None:
    """LZ4-frame compress, or None when no compressor is available."""
    if _lz4frame is not None:
        return _lz4frame.compress(data)
    return None
