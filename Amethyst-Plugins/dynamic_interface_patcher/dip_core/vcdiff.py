"""
VCDIFF (RFC 3284) decoder with the xdelta3 extensions DIP patches use.

DIP's binary patches (``*.bin``) are xdelta3 files.  They are normally made
with LZMA secondary compression and an Adler-32 checksum per window, both of
which are handled here.  DJW/FGK secondary compression and custom code tables
are not supported; :func:`decode` raises :class:`VCDiffUnsupported` for those
so the caller can fall back to the host's ``xdelta3`` binary.
"""

from __future__ import annotations

import lzma
import zlib

VCD_MAGIC = b"\xd6\xc3\xc4"

# header indicator
VCD_DECOMPRESS = 0x01
VCD_CODETABLE = 0x02
VCD_APPHEADER = 0x04
# window indicator
VCD_SOURCE = 0x01
VCD_TARGET = 0x02
VCD_ADLER32 = 0x04
# delta indicator (xdelta3: which sections use secondary compression)
VCD_DATACOMP = 0x01
VCD_INSTCOMP = 0x02
VCD_ADDRCOMP = 0x04

SEC_DJW, SEC_LZMA, SEC_FGK = 1, 2, 16

NOOP, ADD, RUN, COPY = 0, 1, 2, 3
NEAR_SIZE, SAME_SIZE = 4, 3


class VCDiffError(ValueError):
    pass


class VCDiffUnsupported(VCDiffError):
    pass


def _default_code_table() -> list[tuple[int, int, int, int, int, int]]:
    """(type1, size1, mode1, type2, size2, mode2) for each opcode, per RFC 3284 5.6."""
    t: list[tuple[int, int, int, int, int, int]] = [(RUN, 0, 0, NOOP, 0, 0)]
    for size in range(0, 18):
        t.append((ADD, size, 0, NOOP, 0, 0))
    for mode in range(9):
        t.append((COPY, 0, mode, NOOP, 0, 0))
        for size in range(4, 19):
            t.append((COPY, size, mode, NOOP, 0, 0))
    for mode in range(6):
        for add in range(1, 5):
            for size in range(4, 7):
                t.append((ADD, add, 0, COPY, size, mode))
    for mode in range(6, 9):
        for add in range(1, 5):
            t.append((ADD, add, 0, COPY, 4, mode))
    for mode in range(9):
        t.append((COPY, 4, mode, ADD, 1, 0))
    assert len(t) == 256
    return t


CODE_TABLE = _default_code_table()


class _Reader:
    __slots__ = ("buf", "pos")

    def __init__(self, buf: bytes, pos: int = 0):
        self.buf = buf
        self.pos = pos

    def byte(self) -> int:
        try:
            b = self.buf[self.pos]
        except IndexError:
            raise VCDiffError("unexpected end of VCDIFF data") from None
        self.pos += 1
        return b

    def varint(self) -> int:
        value = 0
        for _ in range(10):
            b = self.byte()
            value = (value << 7) | (b & 0x7F)
            if not b & 0x80:
                return value
        raise VCDiffError("VCDIFF integer too long")

    def take(self, n: int) -> bytes:
        if self.pos + n > len(self.buf):
            raise VCDiffError("unexpected end of VCDIFF data")
        out = self.buf[self.pos:self.pos + n]
        self.pos += n
        return out

    def eof(self) -> bool:
        return self.pos >= len(self.buf)


def _secondary(data: bytes, sec_id: int) -> bytes:
    """Undo xdelta3 secondary compression: varint(raw size) + compressed bytes."""
    r = _Reader(data)
    raw_size = r.varint()
    payload = data[r.pos:]
    if sec_id == SEC_LZMA:
        # xdelta3 sync-flushes its xz stream instead of finishing it, so the
        # end-of-stream marker is missing; a streaming decoder copes with that.
        try:
            out = lzma.LZMADecompressor(format=lzma.FORMAT_XZ).decompress(payload, max_length=raw_size)
        except lzma.LZMAError as e:
            raise VCDiffError(f"corrupt LZMA section: {e}") from e
    else:
        raise VCDiffUnsupported(f"secondary compressor id {sec_id} is not supported")
    if len(out) != raw_size:
        raise VCDiffError("secondary decompression produced the wrong size")
    return out


def decode(source: bytes, delta: bytes) -> bytes:
    """Apply an xdelta3/VCDIFF ``delta`` to ``source`` and return the target."""
    r = _Reader(delta)
    if r.take(3) != VCD_MAGIC:
        raise VCDiffError("not a VCDIFF file")
    r.byte()  # version (0, or 'S' for xdelta3's own)
    hdr = r.byte()
    sec_id = 0
    if hdr & VCD_DECOMPRESS:
        sec_id = r.byte()
    if hdr & VCD_CODETABLE:
        raise VCDiffUnsupported("custom VCDIFF code tables are not supported")
    if hdr & VCD_APPHEADER:
        r.take(r.varint())

    target = bytearray()
    while not r.eof():
        win = r.byte()
        if win & VCD_SOURCE and win & VCD_TARGET:
            raise VCDiffError("window uses both source and target segments")
        seg_len = seg_pos = 0
        seg_from_target = False
        if win & (VCD_SOURCE | VCD_TARGET):
            seg_len = r.varint()
            seg_pos = r.varint()
            seg_from_target = bool(win & VCD_TARGET)
        r.varint()  # length of the delta encoding
        tgt_len = r.varint()
        delta_ind = r.byte()
        data_len, inst_len, addr_len = r.varint(), r.varint(), r.varint()
        checksum = None
        if win & VCD_ADLER32:
            checksum = int.from_bytes(r.take(4), "big")
        data_s, inst_s, addr_s = r.take(data_len), r.take(inst_len), r.take(addr_len)
        if delta_ind & (VCD_DATACOMP | VCD_INSTCOMP | VCD_ADDRCOMP) and not sec_id:
            raise VCDiffError("compressed section without a secondary compressor")
        if delta_ind & VCD_DATACOMP:
            data_s = _secondary(data_s, sec_id)
        if delta_ind & VCD_INSTCOMP:
            inst_s = _secondary(inst_s, sec_id)
        if delta_ind & VCD_ADDRCOMP:
            addr_s = _secondary(addr_s, sec_id)

        if seg_len:
            src_buf = target if seg_from_target else source
            if seg_pos + seg_len > len(src_buf):
                raise VCDiffError("source segment exceeds the source file "
                                  "(is this the right original file?)")
            segment = bytes(src_buf[seg_pos:seg_pos + seg_len])
        else:
            segment = b""

        out = _window(segment, data_s, inst_s, addr_s, tgt_len)
        if checksum is not None and (zlib.adler32(out) & 0xFFFFFFFF) != checksum:
            raise VCDiffError("checksum mismatch - the original file does not match this patch")
        target += out
    return bytes(target)


def _window(segment: bytes, data_s: bytes, inst_s: bytes, addr_s: bytes, tgt_len: int) -> bytes:
    seg_len = len(segment)
    out = bytearray()
    data = _Reader(data_s)
    inst = _Reader(inst_s)
    addr = _Reader(addr_s)
    near = [0] * NEAR_SIZE
    near_next = 0
    same = [0] * (SAME_SIZE * 256)

    def address(here: int, mode: int) -> int:
        nonlocal near_next
        if mode == 0:
            a = addr.varint()
        elif mode == 1:
            a = here - addr.varint()
        elif mode < 2 + NEAR_SIZE:
            a = near[mode - 2] + addr.varint()
        else:
            a = same[(mode - 2 - NEAR_SIZE) * 256 + addr.byte()]
        near[near_next] = a
        near_next = (near_next + 1) % NEAR_SIZE
        same[a % (SAME_SIZE * 256)] = a
        return a

    while not inst.eof():
        entry = CODE_TABLE[inst.byte()]
        for typ, size, mode in ((entry[0], entry[1], entry[2]), (entry[3], entry[4], entry[5])):
            if typ == NOOP:
                continue
            if size == 0:
                size = inst.varint()
            if typ == ADD:
                out += data.take(size)
            elif typ == RUN:
                out += bytes((data.byte(),)) * size
            else:  # COPY
                here = seg_len + len(out)
                a = address(here, mode)
                if a < seg_len:
                    end = a + size
                    if end <= seg_len:
                        out += segment[a:end]
                        continue
                    # copy spans source segment and target window
                    out += segment[a:]
                    size = end - seg_len
                    a = seg_len
                t = a - seg_len
                if t + size <= len(out):
                    out += out[t:t + size]
                else:  # overlapping run inside the target window
                    for k in range(size):
                        out.append(out[t + k])
    if len(out) != tgt_len:
        raise VCDiffError(f"window decoded to {len(out)} bytes, expected {tgt_len}")
    return bytes(out)
