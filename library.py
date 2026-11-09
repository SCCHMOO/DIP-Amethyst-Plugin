"""
Profile scanning for the Amethyst panel (no Qt).

Finds DIP patches inside the profile's mods and, for each, the mods that can
serve as the "original mod" (they contain the BSA entries / loose SWFs the
patch changes).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from dip_core.bsa import BSAArchive
from dip_core.fsutil import ci_resolve
from dip_core.patcher import Target, check_patch, patch_targets
from dip_core.tools import windows_dip_ffdec

LogFn = Callable[[str], None]

# folders that never hold a DIP patch but can be huge
_SKIP = {"textures", "meshes", "sound", "music", "video", "scripts", "source", "seq",
         "grass", "lodsettings", "terrain", "strings", "skse", "fomod", "patch", "shapes"}
OUTPUT_SUFFIX = " (DIP Output)"


@dataclass
class ModInfo:
    name: str
    path: Path
    enabled: bool
    priority: int

    @staticmethod
    def from_list(rows: list[dict]) -> list["ModInfo"]:
        out = []
        for r in rows:
            if r.get("kind") != "mod" or not r.get("path") or not r.get("folder_exists", True):
                continue
            out.append(ModInfo(r["name"], Path(r["path"]), bool(r.get("enabled")), int(r.get("priority") or 0)))
        return out


@dataclass
class FoundPatch:
    mod: ModInfo
    root: Path
    targets: list[Target]
    candidates: list[ModInfo] = field(default_factory=list)

    @property
    def label(self) -> str:
        rel = os.path.relpath(self.root, self.mod.path)
        return self.mod.name if rel == "." else f"{self.mod.name} / {rel}"

    @property
    def default_output_name(self) -> str:
        return safe_mod_name(self.mod.name + OUTPUT_SUFFIX)


def safe_mod_name(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip().rstrip(".")
    return name or "DIP Output"


def find_patch_roots(mod_path: Path, max_depth: int = 3) -> list[Path]:
    """Folders (mod root or up to ``max_depth`` levels below) that hold a ``Patch`` folder."""
    roots: list[Path] = []
    stack = [(Path(mod_path), 0)]
    while stack:
        cur, depth = stack.pop()
        try:
            entries = [e for e in os.scandir(cur) if e.is_dir()]
        except OSError:
            continue
        if any(e.name.lower() == "patch" for e in entries) and check_patch(cur):
            roots.append(cur)
            continue
        if depth < max_depth:
            for e in entries:
                if e.name.lower() not in _SKIP:
                    stack.append((Path(e.path), depth + 1))
    return sorted(roots)


class Library:
    def __init__(self, mods: list[ModInfo], log: LogFn = print):
        self.mods = mods
        self.log = log
        self._roots: dict[Path, set[str]] = {}
        self._bsas: dict[Path, Optional[BSAArchive]] = {}

    def _root_names(self, mod: ModInfo) -> set[str]:
        names = self._roots.get(mod.path)
        if names is None:
            try:
                names = {e.name.lower() for e in os.scandir(mod.path)}
            except OSError:
                names = set()
            self._roots[mod.path] = names
        return names

    def _bsa(self, path: Path) -> Optional[BSAArchive]:
        if path not in self._bsas:
            try:
                self._bsas[path] = BSAArchive(path)
            except Exception as e:  # noqa: BLE001
                self.log(f"Could not read {path}: {e}")
                self._bsas[path] = None
        return self._bsas[path]

    def provides(self, mod: ModInfo, t: Target) -> bool:
        first = (t.bsa or t.swf.parts[0]).lower()
        if first not in self._root_names(mod):
            return False
        if t.bsa:
            p = ci_resolve(mod.path, t.bsa)
            arc = self._bsa(p) if p is not None and p.is_file() else None
            return arc is not None and arc.find(str(t.swf)) is not None
        p = ci_resolve(mod.path, t.swf)
        return p is not None and p.is_file()

    def scan(self, cancel: Callable[[], bool] = lambda: False) -> list[FoundPatch]:
        found: list[FoundPatch] = []
        for mod in self.mods:
            if cancel():
                break
            if mod.name.endswith(OUTPUT_SUFFIX):
                continue
            for root in find_patch_roots(mod.path):
                try:
                    targets = patch_targets(root)
                except Exception as e:  # noqa: BLE001
                    self.log(f"Skipping {root}: {e}")
                    continue
                found.append(FoundPatch(mod, root, targets))
        for fp in found:
            req = [t for t in fp.targets if t.required] or fp.targets
            fp.candidates = sorted(
                (m for m in self.mods
                 if m.path != fp.mod.path and not m.name.endswith(OUTPUT_SUFFIX)
                 and all(self.provides(m, t) for t in req)),
                key=lambda m: (not m.enabled, -m.priority))
        return found

    def ffdec_hints(self) -> list[Path]:
        """ffdec.jar of Windows DIP installs among the mods (enabled, higher priority first)."""
        hints = []
        for m in sorted(self.mods, key=lambda m: (not m.enabled, -m.priority)):
            if "dip" in m.name.lower() or "interface patcher" in m.name.lower():
                jar = windows_dip_ffdec(m.path)
                if jar:
                    hints.append(jar)
        return hints
