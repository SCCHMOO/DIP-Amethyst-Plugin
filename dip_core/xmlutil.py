"""
XML helpers, ported unchanged in behaviour from DIP 2.1.5
(``core/utilities/xml_utils.py``) plus a JSON-with-comments loader that
replaces the ``jstyleson`` dependency.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from typing import Any
from xml.dom.minidom import parseString


def split_frames(xml_element: ET.Element) -> ET.Element:
    """Group the children between ShowFrameTag items into numbered <frame> elements."""
    new_frame = ET.Element("frame")
    current_frame = 1
    new_frame.set("frameId", str(current_frame))
    new_frame_subtags = ET.Element("subTags")
    new_frame.append(new_frame_subtags)

    frame_delimiters = xml_element.findall("./item[@type='ShowFrameTag']")

    for child in xml_element.findall("./"):
        child = split_frames(child)
        # (sic) DIP only restructures elements with more than one frame
        if len(frame_delimiters) > 1:
            xml_element.remove(child)
            if child.get("type") != "ShowFrameTag":
                new_frame_subtags.append(child)
            elif child in frame_delimiters:
                xml_element.append(new_frame)
                new_frame = ET.Element("frame")
                current_frame += 1
                new_frame.set("frameId", str(current_frame))
                new_frame_subtags = ET.Element("subTags")
                new_frame.append(new_frame_subtags)

    return xml_element


def unsplit_frames(xml_element: ET.Element) -> ET.Element:
    """Reverse of :func:`split_frames`."""
    for child in xml_element.findall("./"):
        for frame in child.findall("./frame"):
            child.remove(frame)
            for frame_child in frame.findall("./subTags/"):
                child.append(frame_child)
            frame_tag = ET.Element("item")
            frame_tag.set("type", "ShowFrameTag")
            child.append(frame_tag)
        unsplit_frames(child)
    return xml_element


def beautify_xml(xml_string: str) -> str:
    try:
        pretty = parseString(xml_string).toprettyxml()
        return "".join(line + "\n" for line in pretty.splitlines() if line.strip())
    except Exception:  # noqa: BLE001
        return xml_string


def _strip_comments(text: str) -> str:
    """Remove // and /* */ comments and trailing commas outside of strings."""
    out: list[str] = []
    i, n = 0, len(text)
    in_str = False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
        elif text.startswith("//", i) or c == "#" and _line_start(text, i):
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
        else:
            out.append(c)
            i += 1
    s = "".join(out)
    # trailing commas: ", }" / ", ]"
    res: list[str] = []
    in_str = False
    i = 0
    while i < len(s):
        c = s[i]
        if in_str:
            res.append(c)
            if c == "\\" and i + 1 < len(s):
                res.append(s[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
        elif c == '"':
            in_str = True
            res.append(c)
        elif c == ",":
            j = i + 1
            while j < len(s) and s[j] in " \t\r\n":
                j += 1
            if j < len(s) and s[j] in "}]":
                i += 1
                continue
            res.append(c)
        else:
            res.append(c)
        i += 1
    return "".join(res)


def _line_start(text: str, i: int) -> bool:
    j = i - 1
    while j >= 0 and text[j] in " \t":
        j -= 1
    return j < 0 or text[j] == "\n"


def load_jsonc(text: str) -> Any:
    """Parse JSON that may contain comments and trailing commas (like jstyleson)."""
    if text.startswith("﻿"):
        text = text[1:]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return json.loads(_strip_comments(text))
