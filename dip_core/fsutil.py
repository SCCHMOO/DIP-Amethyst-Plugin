"""
File system helpers.

Patches for DIP are authored on Windows, where paths are case-insensitive and
use backslashes.  Everything that maps a patch path onto a mod folder goes
through these helpers so it behaves the same on Linux.
"""

from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path, PurePosixPath
from typing import Iterator, Optional


def norm_rel(path: str | os.PathLike) -> PurePosixPath:
    """Turn a Windows-or-POSIX relative path into a clean POSIX relative path."""
    text = str(path).replace("\\", "/")
    parts = [p for p in text.split("/") if p not in ("", ".")]
    return PurePosixPath(*parts) if parts else PurePosixPath(".")


def ci_resolve(base: Path, rel: str | os.PathLike) -> Optional[Path]:
    """
    Resolve ``rel`` below ``base`` ignoring letter case.

    Returns the real on-disk path, or None when some component does not exist.
    An exact-case hit is tried first so the common case costs one stat.
    """
    cur = Path(base)
    for part in norm_rel(rel).parts:
        if part == "..":
            cur = cur.parent
            continue
        cand = cur / part
        if os.path.lexists(cand):
            cur = cand
            continue
        low = part.lower()
        try:
            with os.scandir(cur) as it:
                match = next((e for e in it if e.name.lower() == low), None)
        except OSError:
            return None
        if match is None:
            return None
        cur = Path(match.path)
    return cur


def ci_dest(base: Path, rel: str | os.PathLike) -> Path:
    """
    Destination path for writing ``rel`` below ``base``.

    Existing directories are reused with their on-disk spelling (so we never
    create ``Interface`` next to ``interface``); missing parts keep the
    spelling of ``rel``.
    """
    cur = Path(base)
    parts = norm_rel(rel).parts
    for i, part in enumerate(parts):
        found = ci_resolve(cur, part)
        if found is None:
            return cur.joinpath(*parts[i:])
        cur = found
    return cur


def _pattern_regex(pattern: str) -> re.Pattern[str]:
    return re.compile(fnmatch.translate(pattern), re.IGNORECASE)


def glob_files(base: Path, pattern: str, recursive: bool = True) -> list[Path]:
    """
    Port of DIP's ``glob.dll``: all regular files below ``base`` whose *file
    name* matches ``pattern`` (case-insensitive), sorted for stable output.
    """
    rx = _pattern_regex(pattern)
    out: list[Path] = []
    if not os.path.isdir(base):
        return out
    if recursive:
        for root, dirs, files in os.walk(base, followlinks=True):
            dirs.sort()
            for f in files:
                if rx.match(f) and os.path.isfile(os.path.join(root, f)):
                    out.append(Path(root) / f)
    else:
        for e in os.scandir(base):
            if e.is_file() and rx.match(e.name):
                out.append(Path(e.path))
    return sorted(out)


def iter_dirs(base: Path, max_depth: int) -> Iterator[Path]:
    """Yield directories below ``base`` up to ``max_depth`` levels deep."""
    stack: list[tuple[Path, int]] = [(Path(base), 0)]
    while stack:
        cur, depth = stack.pop()
        try:
            entries = sorted(os.scandir(cur), key=lambda e: e.name.lower())
        except OSError:
            continue
        for e in entries:
            try:
                if not e.is_dir():
                    continue
            except OSError:
                continue
            p = Path(e.path)
            yield p
            if depth + 1 < max_depth:
                stack.append((p, depth + 1))
