"""
Run DIP patches written for FFDec 18.5.0 against newer FFDec XML exports.

DIP's JSON patches address FFDec's SWF->XML export by tag types, attribute
names and attribute *values*, and DIP.exe bundles FFDec 18.5.0.  Newer FFDec
versions (verified with 26.4.0 on Skyrim UI files) changed the export:

=====================================  ==========================================
FFDec 18.5.0 (what patches expect)     FFDec 26.x
=====================================  ==========================================
``CSMTextSettingsTag``                 ``CSMSettingsTag`` (same attributes)
every attribute written                attributes whose value is unused/default are
                                       omitted (18.5.0 wrote ``0``, ``0.0`` or
                                       ``false`` for them), e.g. ``scaleX`` when
                                       ``hasScale="false"``
MATRIX ``scaleX``, ``scaleY``,         decimals (``1.0113678``)
``rotateSkew0``, ``rotateSkew1`` as
raw 16.16 fixed integers (``66281``)
ZONEDATA ``alignmentCoordinate``,      decimals
``range`` as raw half-float bits
raw strings                            backslash-escaped (``\\``, ``\\n``, ``\\t``,
                                       ``\\uXXXX``); font names keep a trailing NUL
=====================================  ==========================================

:class:`Compat` translates in both directions: filters (``#attr``) are matched
as 18.5.0 would have seen the element, and changes (``~attr``) are converted to
the representation the newer FFDec reads back.  For 18.5.0 XML (no
``_xmlExportMajor`` attribute) it is inactive and DIP's original ElementTree
matching is used unchanged.
"""

from __future__ import annotations

import re
import struct
import xml.etree.ElementTree as ET
from collections import Counter
from typing import Optional

TYPE_ALIASES = {"CSMTextSettingsTag": "CSMSettingsTag"}          # 18.5.0 name -> new name
PLACEHOLDERS = {"0", "0.0", "false"}                             # what 18.5.0 wrote for omitted fields
FIXED_16_16 = {("MATRIX", a) for a in ("scaleX", "scaleY", "rotateSkew0", "rotateSkew1")}
HALF_FLOAT = {("ZONEDATA", "alignmentCoordinate"), ("ZONEDATA", "range")}

_INT = re.compile(r"^[+-]?\d+$")
_STEP = re.compile(r"^([^\[\]/]+)((?:\[@[^=\[\]]+='[^']*'\])*)$")
_PRED = re.compile(r"\[@([^=\[\]]+)='([^']*)'\]")


def escape(value: str) -> str:
    """Raw text -> FFDec 19+ XML string value."""
    out = []
    for ch in value:
        if ch == "\\":
            out.append("\\\\")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 0x20:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    return "".join(out)


def unescape(value: str) -> str:
    """FFDec 19+ XML string value -> raw text."""
    if "\\" not in value:
        return value
    out, i, n = [], 0, len(value)
    simple = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f", "\\": "\\", '"': '"', "'": "'"}
    while i < n:
        ch = value[i]
        if ch == "\\" and i + 1 < n:
            nxt = value[i + 1]
            if nxt in simple:
                out.append(simple[nxt])
                i += 2
                continue
            if nxt == "u" and i + 6 <= n and re.fullmatch(r"[0-9a-fA-F]{4}", value[i + 2:i + 6]):
                out.append(chr(int(value[i + 2:i + 6], 16)))
                i += 6
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def _half_bits_to_float(bits: int) -> float:
    return struct.unpack("<e", (bits & 0xFFFF).to_bytes(2, "little"))[0]


_FB_LIMIT = 1 << 30   # MATRIX scale/rotate are FB[nBits] with nBits <= 31: |fixed| < 2^30 (scale < 16384.0)


def _check_range(key: tuple, attr: str, value: str) -> None:
    """
    Reject values a SWF cannot store.  FFDec 26.x refuses them while 18.5.0
    silently writes 0, so without this the 18.5.0 fallback could hide the error.
    Integers are 18.5.0's raw form, decimals the newer one.
    """
    if key in FIXED_16_16:
        try:
            fixed = int(value) if _INT.match(value) else round(float(value) * 65536)
        except (ValueError, OverflowError):
            return
        if not -_FB_LIMIT <= fixed < _FB_LIMIT:
            raise ValueError(f"{attr}={value} is outside the range a SWF matrix can store "
                             f"(|scale| < 16384.0, or < {_FB_LIMIT} as an FFDec 18.5.0 integer)")
    elif key in HALF_FLOAT and not _INT.match(value):
        try:
            struct.pack("<e", float(value))
        except (ValueError, OverflowError):
            raise ValueError(f"{attr}={value} is outside the range of a 16-bit float (|x| <= 65504)") from None


class Compat:
    """Matching/conversion rules for one XML document."""

    def __init__(self, root: ET.Element):
        major = root.get("_xmlExportMajor")
        self.version = f"{major}.{root.get('_xmlExportMinor', '0')}" if major else None
        self.active = major is not None
        self.stats: Counter[str] = Counter()

    # -- filters ------------------------------------------------------------ #
    def findall(self, root: ET.Element, path: str) -> list[ET.Element]:
        """ElementTree ``findall`` for DIP's filters, with 18.5.0-compatible predicates."""
        steps = self._parse(path) if self.active else None
        if steps is None:
            return root.findall(path)
        current = [root]
        for tag, preds in steps:
            if tag == ".":
                nxt = current
            else:
                nxt = [c for el in current for c in el if tag == "*" or c.tag == tag]
            for attr, value in preds:
                nxt = [el for el in nxt if self.attr_matches(el, attr, value)]
            current = nxt
        return current

    @staticmethod
    def _parse(path: str) -> Optional[list[tuple[str, list[tuple[str, str]]]]]:
        parts, buf, quoted = [], [], False
        for ch in path:
            if ch == "'":
                quoted = not quoted
            if ch == "/" and not quoted:
                parts.append("".join(buf))
                buf = []
            else:
                buf.append(ch)
        parts.append("".join(buf))
        steps = []
        for part in parts:
            m = _STEP.match(part)
            if not m or part in ("", ".."):  # '//' , '..' etc: let ElementTree handle it
                return None
            steps.append((m.group(1), _PRED.findall(m.group(2))))
        return steps

    def attr_matches(self, el: ET.Element, attr: str, value: str) -> bool:
        actual = el.get(attr)
        if actual == value:
            return True
        if not self.active:
            return False
        if attr == "type":
            hit = TYPE_ALIASES.get(value) == actual
            self.stats["tag aliases"] += hit
            return hit
        if actual is None:
            hit = value in PLACEHOLDERS
            self.stats["omitted defaults"] += hit
            return hit
        key = (el.get("type"), attr)
        if key in FIXED_16_16 and _INT.match(value):
            try:
                hit = round(float(actual) * 65536) == int(value)
            except ValueError:
                return False
            self.stats["fixed-point values"] += hit
            return hit
        if key in HALF_FLOAT and _INT.match(value):
            try:
                hit = abs(float(actual) - _half_bits_to_float(int(value))) <= 1e-6 * max(1.0, abs(float(actual)))
            except ValueError:
                return False
            self.stats["half-float values"] += hit
            return hit
        try:  # same number, different formatting ("400" vs "400.0")
            if float(actual) == float(value):
                self.stats["number formats"] += 1
                return True
        except ValueError:
            pass
        hit = unescape(actual).rstrip("\0") == value.rstrip("\0")
        self.stats["escaped strings"] += hit
        return hit

    # -- changes ------------------------------------------------------------ #
    def convert_change(self, el: ET.Element, attr: str, value: str) -> str:
        """Value to write for ``~attr`` so the newer FFDec reads what 18.5.0 would have."""
        key = (el.get("type"), attr)
        _check_range(key, attr, value)
        if not self.active:
            # FFDec 18.5.0 reads these fields as raw integers and silently writes 0 for
            # anything else (e.g. a decimal written for a newer FFDec), so convert decimals.
            if (key in FIXED_16_16 or key in HALF_FLOAT) and not _INT.match(value):
                try:
                    f = float(value)
                except ValueError:
                    return value
                self.stats["decimals for 18.5.0"] += 1
                if key in FIXED_16_16:
                    return str(round(f * 65536))
                return str(int.from_bytes(struct.pack("<e", f), "little"))
            return value
        if key in FIXED_16_16 and _INT.match(value):  # 18.5.0 wrote integers, 26.x writes decimals
            self.stats["fixed-point values"] += 1
            return repr(int(value) / 65536)
        if key in HALF_FLOAT and _INT.match(value):
            self.stats["half-float values"] += 1
            return repr(_half_bits_to_float(int(value)))
        escaped = escape(value)
        self.stats["escaped strings"] += escaped != value
        return escaped

    def convert_type(self, value: str) -> str:
        if self.active and value in TYPE_ALIASES:
            self.stats["tag aliases"] += 1
            return TYPE_ALIASES[value]
        return value

    def summary(self) -> str:
        return ", ".join(f"{n} {k}" for k, n in sorted(self.stats.items()) if n)
