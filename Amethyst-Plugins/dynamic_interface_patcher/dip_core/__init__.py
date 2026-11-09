"""
dip_core - native Linux port of Cutleast's Dynamic Interface Patcher (DIP) engine.

Pure Python, no Qt, no Wine.  The original Windows build bundled a JRE,
7z.exe, xdelta.exe and a C++ glob.dll; this port replaces them with:

* the host's Java runtime (FFDec is a plain jar),
* a built-in VCDIFF/xdelta3 decoder (or the host's ``xdelta3`` if present),
* a built-in BSA reader/writer with LZ4/zlib support,
* case-insensitive path resolution, because Windows-authored patches do not
  care about letter case and Linux file systems do.

Based on DIP v2.1.5, Copyright (c) 2024 Cutleast, GPL-3.0.  This port is
distributed under the same license; see LICENSE.
"""

from .patcher import DipError, Patcher, PatchOptions, check_patch, patch_targets  # noqa: F401

__version__ = "2.1.5.4"
DIP_VERSION = "2.1.5"
