#!/usr/bin/env python3
"""
Dynamic Interface Patcher - native Linux command line.

Same arguments as the Windows DIP.exe:

    dip_cli.py PATCH ORIGINAL [-o OUTPUT] [-b] [-d]

PATCH     the patch folder (the one containing "Patch"), or a mod folder holding one patch
ORIGINAL  the original mod folder (e.g. your RaceMenu mod folder)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dip_core import DIP_VERSION, __version__  # noqa: E402
from dip_core.patcher import (DipError, Patcher, PatchOptions, find_patches,  # noqa: E402
                              mod_provides, patch_targets, resolve_patch_root)
from dip_core.tools import DIP_FFDEC_VERSION, FFDEC_VERSION, ToolError, install_ffdec_zip  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="dip",
        description=f"Dynamic Interface Patcher {__version__} (native Linux port of DIP {DIP_VERSION} "
                    "by Cutleast) - an automated patcher for UI (SWF) files.")
    ap.add_argument("patchpath", nargs="?", help="Path to the patch (folder containing 'Patch').")
    ap.add_argument("originalpath", nargs="?", help="Path to the original mod to patch.")
    ap.add_argument("-o", "--output-path", type=Path,
                    help="Output folder for patched files (default: ./DIP Output).")
    ap.add_argument("-b", "--repack-bsa", action="store_true",
                    help="Experimental: repack the original BSA(s) with the patched files.")
    ap.add_argument("-d", "--debug", action="store_true",
                    help="Write debug files (processed patch JSON and patched XML).")
    ap.add_argument("-s", "--silent", action="store_true", help="Accepted for DIP compatibility (no GUI here).")
    ap.add_argument("--no-backup", action="store_true",
                    help="Overwrite existing output files instead of renaming them with a timestamp.")
    ap.add_argument("--java", default="", help="Java executable (default: auto-detect).")
    ap.add_argument("--ffdec", default="",
                    help="FFDec jar to use as-is, or an ffdec_<version>.zip to install and use.")
    ap.add_argument("--ffdec-version", default=FFDEC_VERSION,
                    help=f"FFDec version to find or download (default {FFDEC_VERSION}; "
                         f"DIP.exe bundles {DIP_FFDEC_VERSION}).")
    ap.add_argument("--no-ffdec-fallback", action="store_true",
                    help=f"Do not retry a SWF with FFDec {DIP_FFDEC_VERSION} when the chosen FFDec fails on it.")
    ap.add_argument("--install-ffdec", metavar="ZIP", type=Path,
                    help="Install an FFDec release zip (e.g. ffdec_26.4.0.zip) for later runs and exit.")
    ap.add_argument("--no-download", action="store_true", help="Never download FFDec.")
    ap.add_argument("--xdelta3", default="",
                    help="xdelta3 executable used for patches the built-in decoder cannot read, "
                         "or 'builtin' to never use one.")
    ap.add_argument("--list", metavar="FOLDER", type=Path,
                    help="List DIP patches found in FOLDER (e.g. your mods/staging folder) and exit.")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__} (DIP {DIP_VERSION})")
    a = ap.parse_args(argv)

    if a.install_ffdec:
        try:
            install_ffdec_zip(a.install_ffdec, log=print)
        except ToolError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        return 0

    if a.list:
        for root in find_patches(a.list, max_depth=4):
            print(root)
            for t in patch_targets(root):
                print(f"    {t.kind:4} -> {t.describe()}{'' if t.required else ' (optional)'}")
        return 0

    if not a.patchpath or not a.originalpath:
        ap.error("PATCH and ORIGINAL are required (or use --list FOLDER)")

    opts = PatchOptions(
        output_folder=(a.output_path or Path.cwd() / "DIP Output").expanduser().resolve(),
        repack_bsa=a.repack_bsa, debug=a.debug, debug_folder=Path.cwd(),
        backup_existing=not a.no_backup, java=a.java, ffdec_jar=a.ffdec, ffdec_version=a.ffdec_version,
        ffdec_fallback=not a.no_ffdec_fallback,
        allow_download=not a.no_download, xdelta3=a.xdelta3)
    try:
        original = Path(a.originalpath).expanduser()
        root = resolve_patch_root(Path(a.patchpath))
        missing = [t.describe() for t in patch_targets(root)
                   if t.required and not mod_provides(original, t)]
        if missing:
            print("Warning: the original mod is missing: " + ", ".join(missing), file=sys.stderr)
        Patcher(opts, log=print).patch(root, original)
    except (DipError, ToolError, OSError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
