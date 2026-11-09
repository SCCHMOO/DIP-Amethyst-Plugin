"""
The DIP patch pipeline, ported from DIP 2.1.5 ``core/patcher/patcher.py``.

A DIP patch is a folder with

    <patch>/Patch/<path>/<file>.json   - XML changes (and shape replacements) for <file>.swf
    <patch>/Patch/<path>/<file>.bin    - xdelta3 binary patch for <file>.swf
    <patch>/Shapes/...                 - replacement shapes/images referenced by the JSON

where ``<path>`` may start with ``<Archive>.bsa/`` to address a file packed in
one of the original mod's BSAs.  Steps (same order as DIP):

0. load patch data            5. patch XMLs
1. find Java/FFDec if needed  6. convert XMLs back to SWFs
2. copy/extract original SWFs 7. apply binary patches (xdelta)
3. replace shapes             8. write output (optionally repack BSAs)
4. convert SWFs to XML
"""

from __future__ import annotations

import shutil
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Optional

from . import tools, vcdiff
from .bsa import BSAArchive, write_archive
from .ffdec_compat import Compat
from .fsutil import ci_dest, ci_resolve, glob_files, iter_dirs, norm_rel
from .xmlutil import beautify_xml, load_jsonc, split_frames, unsplit_frames

LogFn = Callable[[str], None]


class DipError(RuntimeError):
    pass


@dataclass
class PatchOptions:
    output_folder: Path
    repack_bsa: bool = False
    debug: bool = False
    debug_folder: Optional[Path] = None
    backup_existing: bool = True       # DIP renames files it would overwrite
    java: str = ""
    ffdec_jar: str = ""
    ffdec_hints: list[Path] = field(default_factory=list)
    ffdec_version: str = tools.FFDEC_VERSION
    ffdec_fallback: bool = True        # redo a SWF with FFDec 18.5.0 if the configured FFDec fails on it
    allow_download: bool = True
    java_memory: str = "1024m"
    xdelta3: str = ""                  # "" = system xdelta3 if found, "builtin" = never use it


@dataclass
class Target:
    """One SWF a patch changes."""
    patch_file: Path
    bsa: Optional[str]        # archive name, e.g. "RaceMenu.bsa", or None for a loose file
    swf: PurePosixPath        # path of the SWF (inside the BSA, or relative to the mod)
    kind: str                 # "json" or "bin"
    required: bool = True

    def describe(self) -> str:
        return f"{self.bsa}/{self.swf}" if self.bsa else str(self.swf)


@dataclass
class PatchResult:
    written: list[Path]
    duration: float
    skipped: list[str]
    ffdec_used: dict[str, str] = field(default_factory=dict)   # JSON-patched SWF -> FFDec version kept


# errors of one FFDec pass that the 18.5.0 fallback may fix (cancellation is never retried)
_PASS_ERRORS = (tools.ToolError, DipError, ET.ParseError, OSError, ValueError, KeyError)


def _short(err: Optional[BaseException]) -> str:
    text = str(err) if err is not None else ""
    return text if len(text) <= 160 else text[:157] + "..."


# --------------------------------------------------------------------------- #
# Patch discovery
# --------------------------------------------------------------------------- #
def patch_dir_of(folder: Path) -> Optional[Path]:
    """``folder/Patch`` (any case) if it contains .json or .bin patch files."""
    p = ci_resolve(folder, "Patch")
    if p is None or not p.is_dir():
        return None
    if glob_files(p, "*.json") or glob_files(p, "*.bin"):
        return p
    return None


def check_patch(folder: Path) -> bool:
    return patch_dir_of(folder) is not None


def find_patches(base: Path, max_depth: int = 3) -> list[Path]:
    """Patch roots (folders containing ``Patch/``) at or below ``base``."""
    base = Path(base)
    found = [base] if check_patch(base) else []
    for d in iter_dirs(base, max_depth):
        if d.name.lower() in ("patch", "shapes"):
            continue
        if check_patch(d):
            found.append(d)
    return found


def resolve_patch_root(path: Path) -> Path:
    """Accept the patch folder, its ``Patch`` subfolder, or a mod containing one patch."""
    path = Path(path).expanduser()
    if check_patch(path):  # first: a folder that *contains* "Patch" (even if it is itself named "patch")
        return path
    if path.name.lower() == "patch" and check_patch(path.parent):
        return path.parent
    found = find_patches(path)
    if len(found) == 1:
        return found[0]
    if not found:
        raise DipError(f"No DIP patch (a 'Patch' folder with .json/.bin files) found in {path}")
    raise DipError(f"{path} contains several patches, pick one: " + ", ".join(str(f) for f in found))


def _split_bsa(rel: PurePosixPath) -> tuple[Optional[str], PurePosixPath]:
    """'RaceMenu.bsa/interface/x.bin' -> ('RaceMenu.bsa', 'interface/x.bin') (last .bsa wins, like DIP)."""
    bsa: Optional[str] = None
    rest: list[str] = []
    for part in rel.parts:
        rest.append(part)
        if part.lower().endswith(".bsa"):
            bsa = part
            rest.clear()
    return bsa, PurePosixPath(*rest) if rest else PurePosixPath(".")


def patch_targets(patch_root: Path) -> list[Target]:
    pdir = patch_dir_of(patch_root)
    if pdir is None:
        raise DipError(f"{patch_root} is not a DIP patch")
    out: list[Target] = []
    for kind in ("json", "bin"):
        for f in glob_files(pdir, f"*.{kind}"):
            bsa, rel = _split_bsa(norm_rel(f.relative_to(pdir)))
            required = True
            if kind == "json":
                try:
                    data = load_jsonc(f.read_text(encoding="utf-8-sig"))
                    required = not (isinstance(data, dict) and data.get("optional"))
                except Exception:  # noqa: BLE001 - reported properly when patching
                    pass
            out.append(Target(f, bsa, rel.with_suffix(".swf"), kind, required))
    return out


def mod_provides(mod_folder: Path, target: Target) -> bool:
    """Does ``mod_folder`` contain what ``target`` needs (the BSA entry or the loose SWF)?"""
    if target.bsa:
        bsa = ci_resolve(mod_folder, target.bsa)
        if bsa is None or not bsa.is_file():
            return False
        try:
            return BSAArchive(bsa).find(str(target.swf)) is not None
        except Exception:  # noqa: BLE001
            return False
    f = ci_resolve(mod_folder, target.swf)
    return f is not None and f.is_file()


# --------------------------------------------------------------------------- #
# Patcher
# --------------------------------------------------------------------------- #
class Patcher:
    def __init__(self, options: PatchOptions, log: LogFn = print,
                 cancel: Optional[threading.Event] = None):
        self.opt = options
        self.log = log
        self.cancel = cancel or threading.Event()
        self.tmp: Optional[Path] = None
        self.ffdec: Optional[tools.FFDec] = None
        self._fallback: Optional[tools.FFDec] = None
        self._fallback_note = ""

    # -- helpers ------------------------------------------------------------ #
    def _check_cancel(self) -> None:
        if self.cancel.is_set():
            raise tools.Cancelled("Cancelled.")

    def _tmp(self) -> Path:
        if self.tmp is None:
            self.tmp = Path(tempfile.mkdtemp(prefix="DIP_"))
        return self.tmp

    def clean(self) -> None:
        if self.tmp is not None:
            shutil.rmtree(self.tmp, ignore_errors=True)
            self.tmp = None

    def _setup_ffdec(self) -> tools.FFDec:
        if self.ffdec is not None:
            return self.ffdec
        java = tools.find_java(self.opt.java)
        if not java:
            raise DipError(tools.JAVA_HELP if not self.opt.java
                           else f"Java executable not found: {self.opt.java}")
        try:
            jar = tools.resolve_ffdec(self.log, self.opt.ffdec_jar, self.opt.ffdec_hints,
                                      self.opt.ffdec_version, self.opt.allow_download)
        except tools.ToolError as e:
            raise DipError(str(e)) from e
        self.log(f"Using Java {java} and FFDec {tools.jar_version(jar) or '(unknown version)'} ({jar})")
        self.ffdec = tools.FFDec(java, jar, self.opt.java_memory, self.log, self.cancel)
        return self.ffdec

    # -- pipeline ----------------------------------------------------------- #
    def patch(self, patch_path: Path, original_mod_path: Path) -> PatchResult:
        start = time.time()
        root = resolve_patch_root(patch_path)
        original = Path(original_mod_path).expanduser()
        if not original.is_dir():
            raise DipError(f"Original mod folder does not exist: {original}")
        shapes_dir = ci_resolve(root, "Shapes") or (root / "Shapes")
        self.log(f"Patch: {root}")
        self.log(f"Original mod: {original}")
        self.log(f"Output: {self.opt.output_folder}")

        try:
            # 0. load patch data
            targets = patch_targets(root)
            data: dict[Path, Optional[dict]] = {}
            for t in targets:
                if t.kind == "json":
                    try:
                        data[t.patch_file] = load_jsonc(t.patch_file.read_text(encoding="utf-8-sig"))
                    except Exception as e:  # noqa: BLE001
                        raise DipError(f"Could not read {t.patch_file.name}: {e}") from e
                else:
                    data[t.patch_file] = None
            self.log(f"Loaded {len(targets)} patch file(s).")

            # 1. Java/FFDec only when there are JSON patches
            if any(t.kind == "json" for t in targets):
                self._setup_ffdec()

            # 2. copy / extract original files
            swfs, skipped = self._copy_files(targets, original)
            targets = [t for t in targets if t.patch_file in swfs]
            self._check_cancel()

            # 3.-6. JSON patches, one SWF at a time: shapes, swf -> xml -> patch -> swf.
            # A SWF the configured FFDec fails on is redone with FFDec 18.5.0 (see _patch_json).
            used: dict[str, str] = {}
            for t in targets:
                if t.kind == "json":
                    swf = swfs[t.patch_file]
                    used[t.describe()] = self._patch_json(swf, data[t.patch_file] or {}, shapes_dir)
                    self._check_cancel()
            fell_back = {k: v for k, v in used.items() if self.ffdec and v != self.ffdec.version}
            if fell_back:
                self.log("FFDec fallback used for: " + ", ".join(f"{k} ({v})" for k, v in fell_back.items()))

            # 7. binary patches
            bins = [t for t in targets if t.kind == "bin"]
            if bins:
                self.log(f"Applying {len(bins)} binary patch(es)...")
            for t in bins:
                self._apply_xdelta(swfs[t.patch_file], t.patch_file)
                self._check_cancel()

            # 8. output
            written = self._finish(targets, swfs, original)
        finally:
            self.clean()

        duration = time.time() - start
        self.log(f"Patching complete in {duration:.2f} s; {len(written)} file(s) written.")
        return PatchResult(written, duration, skipped, used)

    # -- JSON patches with FFDec fallback ----------------------------------- #
    def _json_pass(self, ffdec: tools.FFDec, swf: Path, patch: dict, shapes_dir: Path) -> int:
        """Shapes, SWF -> XML, XML edits, XML -> SWF on ``swf`` in place.  Returns unmatched filters."""
        self._patch_shapes(ffdec, swf, patch, shapes_dir)
        self._check_cancel()
        xml = swf.with_suffix(".xml")
        try:
            self.log(f"Converting {swf.name} to XML (FFDec {ffdec.version})...")
            ffdec.swf2xml(swf, xml)
            self._check_cancel()
            unmatched = self._patch_xml(xml, patch.get("swf"))
            self.log(f"Converting {xml.name} back to SWF...")
            ffdec.xml2swf(xml, swf)
            return unmatched
        finally:
            xml.unlink(missing_ok=True)

    def _patch_json(self, swf: Path, patch: dict, shapes_dir: Path) -> str:
        """
        Apply one JSON patch.  If the configured FFDec raises an error or leaves
        patch filters unmatched, start again from the original SWF with FFDec
        18.5.0 (what DIP.exe bundles) and keep whichever result is better.
        Returns the FFDec version whose result was kept.
        """
        primary = self._setup_ffdec()
        pristine = swf.with_name(swf.name + ".pristine")
        first = swf.with_name(swf.name + ".first")
        shutil.copyfile(swf, pristine)
        try:
            err: Optional[BaseException] = None
            unmatched = 0
            try:
                unmatched = self._json_pass(primary, swf, patch, shapes_dir)
            except tools.Cancelled:
                raise
            except _PASS_ERRORS as e:
                err = e
            if err is None and not unmatched:
                return primary.version

            fallback = self._fallback_ffdec()
            if fallback is None:
                if err is not None:
                    raise DipError(f"{swf.name}: FFDec {primary.version} failed: {err}") from err
                if self._fallback_note in ("disabled", "unavailable"):
                    self.log(f"{unmatched} filter(s) matched nothing in {swf.name}; FFDec "
                             f"{tools.DIP_FFDEC_VERSION} might apply them (fallback {self._fallback_note}).")
                return primary.version

            why = f"failed ({_short(err)})" if err else f"left {unmatched} patch filter(s) unmatched"
            self.log(f"FFDec {primary.version} {why} on {swf.name}; "
                     f"retrying from the original file with FFDec {fallback.version}...")
            if err is None:
                swf.replace(first)            # keep the primary result in case 18.5.0 does worse
            shutil.copyfile(pristine, swf)
            fb_err: Optional[BaseException] = None
            fb_unmatched = 0
            try:
                fb_unmatched = self._json_pass(fallback, swf, patch, shapes_dir)
            except tools.Cancelled:
                raise
            except _PASS_ERRORS as e:
                fb_err = e

            if fb_err is None and (err is not None or fb_unmatched < unmatched):
                note = f"; {fb_unmatched} filter(s) still unmatched" if fb_unmatched else ""
                self.log(f"Used FFDec {fallback.version} for {swf.name}{note}.")
                if err is not None:
                    self.log(f"Warning: FFDec {fallback.version} accepted what FFDec {primary.version} rejected. "
                             f"It is more lenient and can silently write defaults for invalid values, "
                             f"so check {swf.name} in game.")
                return fallback.version
            if err is not None:
                raise DipError(f"{swf.name}: FFDec {primary.version} failed ({_short(err)}) and "
                               f"FFDec {fallback.version} failed too ({_short(fb_err)})") from fb_err
            first.replace(swf)
            self.log(f"FFDec {fallback.version} did no better on {swf.name} "
                     f"({_short(fb_err) if fb_err else f'{fb_unmatched} unmatched'}); "
                     f"keeping the FFDec {primary.version} result.")
            return primary.version
        finally:
            pristine.unlink(missing_ok=True)
            first.unlink(missing_ok=True)

    def _fallback_ffdec(self) -> Optional[tools.FFDec]:
        """FFDec 18.5.0 for retries, or None when disabled, not needed or unavailable."""
        if self._fallback is not None or self._fallback_note:
            return self._fallback
        want = tools.DIP_FFDEC_VERSION
        if not self.opt.ffdec_fallback:
            self._fallback_note = "disabled"
        elif self.ffdec is None or self.ffdec.version == want:
            self._fallback_note = "not needed"
        else:
            try:
                jar = tools.find_exact_ffdec(self.log, self.opt.ffdec_hints, want, self.opt.allow_download)
            except tools.ToolError as e:
                jar = None
                self.log(f"Warning: {e}")
            if jar is None:
                self._fallback_note = "unavailable"
                self.log(f"FFDec {want} is not available for the fallback "
                         "(enable downloads or install it with --install-ffdec).")
            else:
                self.log(f"Fallback FFDec {want}: {jar}")
                self._fallback = tools.FFDec(self.ffdec.java, jar, self.opt.java_memory, self.log, self.cancel)
        return self._fallback

    def _copy_files(self, targets: list[Target], original: Path) -> tuple[dict[Path, Path], list[str]]:
        self.log("Copying mod files...")
        swfs: dict[Path, Path] = {}
        skipped: list[str] = []
        archives: dict[Path, BSAArchive] = {}
        for t in targets:
            dest = self._tmp() / t.swf
            if t.bsa is None:
                src = ci_resolve(original, t.swf)
                if src is None or not src.is_file():
                    if t.required:
                        raise DipError(f"'{t.swf}' is required but does not exist in {original}")
                    self.log(f"Warning: '{t.swf}' does not exist; skipped optional patch file.")
                    skipped.append(t.describe())
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dest)
            else:
                bsa_path = ci_resolve(original, t.bsa)
                if bsa_path is None or not bsa_path.is_file():
                    if t.required:
                        raise DipError(f"'{t.bsa}' is required but does not exist in {original}")
                    self.log(f"Warning: '{t.bsa}' does not exist; skipped optional patch file.")
                    skipped.append(t.describe())
                    continue
                arc = archives.get(bsa_path) or archives.setdefault(bsa_path, BSAArchive(bsa_path))
                if arc.find(str(t.swf)) is None:
                    if t.required:
                        raise DipError(f"'{t.swf}' is not in {bsa_path.name}")
                    self.log(f"Warning: '{t.swf}' is not in {bsa_path.name}; skipped optional patch file.")
                    skipped.append(t.describe())
                    continue
                arc.extract(str(t.swf), dest)
            swfs[t.patch_file] = dest
        self.log("Mod files ready to patch.")
        return swfs, skipped

    def _patch_shapes(self, ffdec: tools.FFDec, swf: Path, patch: dict, shapes_dir: Path) -> None:
        shapes: dict[Path, list[int]] = {}
        for shape in patch.get("shapes", []) or []:
            name = str(shape.get("fileName", ""))
            path = ci_resolve(shapes_dir, name)
            if path is None or not path.is_file():
                self.log(f"Error: shape '{shape.get('id')}': file '{shapes_dir / norm_rel(name)}' does not exist!")
                continue
            if path.suffix.lower() not in (".svg", ".png", ".jpg", ".jpeg"):
                self.log(f"Warning: shape file type '{path.suffix}' ({path.name}) is untested.")
            ids = [int(x) for x in str(shape["id"]).split(",") if x.strip()]
            shapes.setdefault(path.resolve(), []).extend(ids)
        if not shapes:
            return
        self.log(f"Patching shapes of {swf.name}...")
        cmd = swf.with_name(swf.stem + ".shapes.txt")
        cmd.write_text("".join(f"{i}\n{p}\n" for p, ids in shapes.items() for i in ids), encoding="utf-8")
        try:
            ffdec.replace(swf, cmd)
        finally:
            cmd.unlink(missing_ok=True)
        self.log("Shapes patched.")

    def _patch_xml(self, xml_file: Path, patch_data: Any) -> int:
        """Apply the patch's XML edits; returns how many filters matched nothing."""
        if not patch_data:
            return 0
        unmatched = 0
        self.log(f"Patching {xml_file.name}...")
        tree = ET.parse(str(xml_file))
        root = tree.getroot()
        items = process_patch_data(patch_data)
        if self.opt.debug:
            self._debug_write(f"{xml_file.stem}.json", __import__("json").dumps(items, indent=4))
        compat = Compat(root)  # translates 18.5.0-style patches for newer FFDec XML; inert for 18.5.0
        root = split_frames(root)
        for item in items:
            filt = "." + str(item.get("filter", ""))
            changes = item.get("changes", {})
            elements = compat.findall(root, filt)
            if not elements:
                parent_filter, tag = filt.rsplit("/", 1)
                parents = compat.findall(root, parent_filter)
                if "[" in tag or not parents:
                    # DIP would append an element literally named e.g. "item[@type='X']" (invalid
                    # XML; a known TODO upstream) or silently do nothing.  Skip and say so.
                    unmatched += 1
                    self.log(f"Warning: {item.get('filter')} matched nothing; change skipped.")
                    continue
                new = ET.Element(tag)
                if "type" in changes:  # set first, so the type-specific conversions below apply
                    new.set("type", compat.convert_type(str(changes["type"])))
                for k, v in changes.items():
                    if k != "type":
                        new.set(k, compat.convert_change(new, k, str(v)))
                for parent in parents:
                    parent.append(new)
            for el in elements:
                for k, v in changes.items():
                    v = str(v)
                    el.attrib[k] = compat.convert_type(v) if k == "type" else compat.convert_change(el, k, v)
        if compat.summary():
            self.log(f"FFDec XML format {compat.version or '(' + tools.DIP_FFDEC_VERSION + ')'}: "
                     f"translated patch values ({compat.summary()}).")
        unsplit_frames(root)
        with open(xml_file, "wb") as f:
            tree.write(f, encoding="utf8")
        if self.opt.debug:
            self._debug_write(xml_file.name, beautify_xml(ET.tostring(tree.getroot(), encoding="unicode")))
        return unmatched

    def _debug_write(self, name: str, text: str) -> None:
        folder = self.opt.debug_folder or Path.cwd()
        folder.mkdir(parents=True, exist_ok=True)
        (folder / name).write_text(text, encoding="utf-8")
        self.log(f"Debug file written: {folder / name}")

    def _apply_xdelta(self, swf: Path, patch: Path) -> None:
        self.log(f"Patching {swf.name} with xdelta...")
        out = swf.with_suffix(".patched")
        binary = None if self.opt.xdelta3 == "builtin" else tools.find_xdelta3(self.opt.xdelta3)
        try:
            out.write_bytes(vcdiff.decode(swf.read_bytes(), patch.read_bytes()))
        except vcdiff.VCDiffUnsupported as e:
            if not binary:
                raise DipError(f"{patch.name}: {e}. Install xdelta3 to apply this patch.") from e
            self.log(f"{e}; using {binary}")
            tools.run_xdelta3(binary, swf, patch, out)
        except vcdiff.VCDiffError as e:
            raise DipError(f"{patch.name}: {e}") from e
        out.replace(swf)
        self.log(f"{swf.name} patched.")

    def _finish(self, targets: list[Target], swfs: dict[Path, Path], original: Path) -> list[Path]:
        out_root = Path(self.opt.output_folder)
        out_root.mkdir(parents=True, exist_ok=True)
        written: list[Path] = []
        by_bsa: dict[str, list[Target]] = {}
        for t in targets:
            if self.opt.repack_bsa and t.bsa:
                by_bsa.setdefault(t.bsa, []).append(t)
                continue
            dest = ci_dest(out_root, swfs[t.patch_file].relative_to(self._tmp()))
            self._place(swfs[t.patch_file], dest)
            written.append(dest)

        for bsa_name, ts in by_bsa.items():
            src_bsa = ci_resolve(original, bsa_name)
            assert src_bsa is not None
            self.log(f"Repacking {src_bsa.name} with patched files...")
            arc = BSAArchive(src_bsa)
            work = self._tmp() / ("__repack_" + src_bsa.name)
            files: dict[str, Path] = {}
            for entry in arc.entries.values():
                files[entry.path] = arc.extract(entry.path, work / entry.path)
            for t in ts:
                key = next((k for k in files if k.lower() == str(t.swf).lower()), str(t.swf))
                files[key] = swfs[t.patch_file]
            dest = ci_dest(out_root, src_bsa.name)
            tmp_out = self._tmp() / src_bsa.name
            version = arc.version if arc.version in (104, 105) else 105
            write_archive(files, tmp_out, version=version, compress=arc.compressed_default,
                          file_flags=arc.file_flags)
            self._place(tmp_out, dest)
            written.append(dest)
        self.log(f"Output written to {out_root}")
        return written

    def _place(self, src: Path, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists() and self.opt.backup_existing:
            backup = dest.with_suffix(dest.suffix + time.strftime(".%d-%m-%Y-%H-%M-%S"))
            shutil.move(dest, backup)
            self.log(f"Backed up existing {dest.name} to {backup.name}")
        elif dest.is_symlink():
            dest.unlink()
        shutil.copyfile(src, dest)


def process_patch_data(patch_data: dict | list) -> list[dict[str, Any]]:
    """
    Flatten DIP's nested patch JSON into ``{"filter": xpath, "changes": {...}}``
    items.  Ported verbatim from ``Patcher.process_patch_data``:

    * ``"#attr": value`` narrows the filter to ``[@attr='value']``
      (``#frameId`` switches the last step to ``frame``),
    * ``"~attr"``/``"attr": value`` sets an attribute,
    * nested objects add ``/key`` steps, lists add ``/item`` steps.
    """
    result: list[dict[str, Any]] = []

    def process(data: Any, cur_filter: str = "") -> dict[str, Any]:
        cur_result: dict[str, Any] = {}
        cur_changes: dict[str, str] = {}
        if isinstance(data, list):
            for item in data:
                if child := process(item, cur_filter + "/item"):
                    result.append(child)
        else:
            for key, value in data.items():
                if isinstance(value, str):
                    if key.startswith("#"):
                        attribute = key.removeprefix("#")
                        if attribute == "frameId":
                            cur_filter, _ = cur_filter.rsplit("/", 1)
                            cur_filter += "/frame"
                        cur_filter += f"[@{attribute}='{value}']"
                    else:
                        cur_changes[key.removeprefix("~")] = value
                    if cur_filter and cur_changes:
                        cur_result = {"filter": cur_filter, "changes": cur_changes}
                else:
                    if child := process(value, cur_filter + f"/{key}"):
                        result.append(child)
        return cur_result

    if top := process(patch_data):
        result.append(top)
    return result
