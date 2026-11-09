"""
Bethesda BSA archives (TES4 family): read v103/v104/v105, write v104/v105.

DIP needs to pull SWF files out of mod BSAs (e.g. ``RaceMenu.bsa``) and, with
the experimental "repack BSA" option, write them back.  File lookups are
case-insensitive and accept either slash direction.
"""

from __future__ import annotations

import os
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Optional

from . import lz4frame

# archive flags
F_DIR_NAMES = 0x1
F_FILE_NAMES = 0x2
F_COMPRESSED = 0x4
F_RETAIN_DIR_NAMES = 0x8
F_RETAIN_FILE_NAMES = 0x10
F_RETAIN_FILE_NAME_OFFSETS = 0x20
F_XBOX = 0x40
F_EMBED_NAMES = 0x100

# file flags (content types)
FF_MESHES, FF_TEXTURES, FF_MENUS, FF_SOUNDS, FF_VOICES = 0x1, 0x2, 0x4, 0x8, 0x10
FF_SHADERS, FF_TREES, FF_FONTS, FF_MISC = 0x20, 0x40, 0x80, 0x100

SIZE_COMPRESS_BIT = 0x40000000
SIZE_MASK = 0x3FFFFFFF
HEADER_SIZE = 36


class BSAError(ValueError):
    pass


def _key(name: str) -> str:
    return name.replace("\\", "/").strip("/").lower()


def tes_hash(name: str, is_folder: bool = False) -> int:
    """TES4/Skyrim BSA name hash (UESP algorithm)."""
    name = name.replace("/", "\\").lower()
    if is_folder:
        root, ext = name, ""
    else:
        root, ext = os.path.splitext(name)
    rb = root.encode("cp1252", "replace")
    eb = ext.encode("cp1252", "replace")
    n = len(rb)
    h1 = 0
    if n:
        h1 = rb[-1] | ((rb[-2] if n > 2 else 0) << 8) | (n << 16) | (rb[0] << 24)
    h1 |= {".kf": 0x80, ".nif": 0x8000, ".dds": 0x8080, ".wav": 0x80000000}.get(ext, 0)
    h2 = 0
    for c in rb[1:-2]:
        h2 = (h2 * 0x1003F + c) & 0xFFFFFFFF
    h3 = 0
    for c in eb:
        h3 = (h3 * 0x1003F + c) & 0xFFFFFFFF
    h2 = (h2 + h3) & 0xFFFFFFFF
    return (h2 << 32) | h1


@dataclass
class BSAEntry:
    path: str          # as stored, "/"-separated
    size_field: int
    offset: int

    @property
    def stored_size(self) -> int:
        return self.size_field & SIZE_MASK


class BSAArchive:
    """Read-only view of a BSA archive."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.entries: dict[str, BSAEntry] = {}
        self._parse()

    # -- parsing ------------------------------------------------------------ #
    def _parse(self) -> None:
        with open(self.path, "rb") as f:
            head = f.read(HEADER_SIZE)
            if len(head) < HEADER_SIZE or head[:4] != b"BSA\x00":
                raise BSAError(f"{self.path.name}: not a BSA archive")
            (self.version, offset, self.archive_flags, folder_count, file_count,
             _fold_len, _file_len, self.file_flags, _pad) = struct.unpack_from("<IIIIIIIHH", head, 4)
            if self.version not in (103, 104, 105):
                raise BSAError(f"{self.path.name}: unsupported BSA version {self.version}")
            f.seek(offset)
            folders: list[int] = []  # file counts
            rec = 24 if self.version == 105 else 16
            for _ in range(folder_count):
                raw = f.read(rec)
                folders.append(struct.unpack_from("<I", raw, 8)[0])
            records: list[tuple[str, int, int]] = []
            for count in folders:
                folder = ""
                if self.archive_flags & F_DIR_NAMES:
                    ln = f.read(1)[0]
                    folder = f.read(ln).rstrip(b"\x00").decode("cp1252", "replace")
                for _ in range(count):
                    _h, size, off = struct.unpack("<QII", f.read(16))
                    records.append((folder, size, off))
            names: list[str] = []
            if self.archive_flags & F_FILE_NAMES:
                blob = f.read(_file_len)
                names = [n.decode("cp1252", "replace") for n in blob.split(b"\x00")[:file_count]]
        for i, (folder, size, off) in enumerate(records):
            name = names[i] if i < len(names) else f"file_{i:06d}"
            rel = (folder.replace("\\", "/") + "/" + name).strip("/")
            self.entries[_key(rel)] = BSAEntry(rel, size, off)

    # -- queries ------------------------------------------------------------ #
    @property
    def compressed_default(self) -> bool:
        return bool(self.archive_flags & F_COMPRESSED)

    def files(self) -> list[str]:
        return [e.path for e in self.entries.values()]

    def find(self, name: str | os.PathLike) -> Optional[BSAEntry]:
        return self.entries.get(_key(str(name)))

    def __contains__(self, name: object) -> bool:
        return isinstance(name, (str, os.PathLike)) and self.find(name) is not None

    # -- extraction --------------------------------------------------------- #
    def read(self, name: str | os.PathLike) -> bytes:
        entry = self.find(name)
        if entry is None:
            raise FileNotFoundError(f"{name!s} is not in {self.path.name}")
        compressed = self.compressed_default ^ bool(entry.size_field & SIZE_COMPRESS_BIT)
        size = entry.stored_size
        with open(self.path, "rb") as f:
            f.seek(entry.offset)
            if self.version >= 104 and self.archive_flags & F_EMBED_NAMES:
                ln = f.read(1)[0]
                f.read(ln)
                size -= ln + 1
            if compressed:
                raw_size = struct.unpack("<I", f.read(4))[0]
                payload = f.read(size - 4)
                data = lz4frame.decompress(payload) if self.version == 105 else zlib.decompress(payload)
                if len(data) != raw_size:
                    raise BSAError(f"{entry.path}: decompressed size mismatch")
                return data
            return f.read(size)

    def extract(self, name: str | os.PathLike, dest: Path) -> Path:
        """Write ``name`` to the exact file path ``dest``."""
        data = self.read(name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return dest

    def extract_all(self, dest_folder: Path) -> None:
        for entry in self.entries.values():
            self.extract(entry.path, Path(dest_folder) / entry.path)


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #
def _content_flags(paths: Iterable[str]) -> int:
    flags = 0
    for p in paths:
        parts = p.lower().split("/")
        top = parts[0] if parts else ""
        if top == "meshes":
            flags |= FF_MESHES
        elif top == "textures":
            flags |= FF_TEXTURES
        elif top == "interface":
            flags |= FF_MENUS
        elif top == "sound":
            flags |= FF_SOUNDS | (FF_VOICES if len(parts) > 2 and parts[1] == "voice" else 0)
        elif top == "shadersfx":
            flags |= FF_SHADERS
        elif top == "fonts":
            flags |= FF_FONTS
        else:
            flags |= FF_MISC
    return flags


def write_archive(files: dict[str, Path], output: Path, version: int = 105,
                  compress: bool = True, file_flags: Optional[int] = None) -> None:
    """
    Create a BSA from ``files`` (archive path -> source file).

    Folders and files are sorted by hash as the engine requires.  When
    compression is requested but no compressor is available (LZ4 for v105)
    the archive is written uncompressed, which the game accepts.
    """
    if version not in (104, 105):
        raise BSAError("only BSA versions 104 and 105 can be written")
    if not files:
        raise BSAError("no files to pack")

    folders: dict[str, list[tuple[str, Path]]] = {}
    for arc_path, src in files.items():
        p = PurePosixPath(_key(arc_path))
        folder = str(p.parent).replace("/", "\\") if str(p.parent) != "." else ""
        folders.setdefault(folder, []).append((p.name, Path(src)))

    if compress and version == 105 and lz4frame.compress(b"") is None:
        compress = False

    ordered = sorted(folders.items(), key=lambda kv: tes_hash(kv[0], True))
    ordered = [(fo, sorted(fl, key=lambda x: tes_hash(x[0]))) for fo, fl in ordered]

    file_names = b"".join(n.encode("cp1252") + b"\x00" for _, fl in ordered for n, _ in fl)
    folder_names_len = sum(len(fo.encode("cp1252")) + 1 for fo, _ in ordered)
    file_count = sum(len(fl) for _, fl in ordered)
    flags = F_DIR_NAMES | F_FILE_NAMES | (F_COMPRESSED if compress else 0)
    rec = 24 if version == 105 else 16

    blocks_len = sum(1 + len(fo.encode("cp1252")) + 1 + 16 * len(fl) for fo, fl in ordered)
    data_start = HEADER_SIZE + rec * len(ordered) + blocks_len + len(file_names)

    payloads: list[list[tuple[int, int]]] = []  # per folder: (size_field, offset)
    with open(output, "wb") as out:
        out.write(b"\x00" * data_start)
        pos = data_start
        for _fo, fl in ordered:
            row = []
            for _name, src in fl:
                raw = src.read_bytes()
                if compress:
                    comp = lz4frame.compress(raw) if version == 105 else zlib.compress(raw)
                    blob = struct.pack("<I", len(raw)) + comp
                else:
                    blob = raw
                out.write(blob)
                row.append((len(blob), pos))
                pos += len(blob)
            payloads.append(row)

        out.seek(0)
        out.write(b"BSA\x00")
        out.write(struct.pack("<IIIIIIIHH", version, HEADER_SIZE, flags, len(ordered), file_count,
                              folder_names_len, len(file_names),
                              _content_flags(files.keys()) if file_flags is None else file_flags, 0))
        # folder records; their offset points at the folder's file record block
        # (+ total file name length, a quirk of the format)
        block_pos = HEADER_SIZE + rec * len(ordered)
        for fo, fl in ordered:
            h = tes_hash(fo, True)
            off = block_pos + len(file_names)
            if version == 105:
                out.write(struct.pack("<QIIQ", h, len(fl), 0, off))
            else:
                out.write(struct.pack("<QII", h, len(fl), off))
            block_pos += 1 + len(fo.encode("cp1252")) + 1 + 16 * len(fl)
        for (fo, fl), row in zip(ordered, payloads):
            name = fo.encode("cp1252") + b"\x00"
            out.write(bytes((len(name),)) + name)
            for (fname, _src), (size, off) in zip(fl, row):
                out.write(struct.pack("<QII", tes_hash(fname), size, off))
        out.write(file_names)
