"""
Automatic re-patching before the game launches (no Qt).

The native equivalent of the "Automatic Patcher" SKSE plugin, done the Amethyst
way: Amethyst calls :func:`before_launch` after deploying and before starting
the game.  For every enabled ``<patch mod> (DIP Output)`` mod it checks whether
the patch files or the original SWFs (in the currently winning original mod)
changed since the output was made, or whether the output files were modified.
If so it patches again and rewrites the output files *in place*, so Amethyst's
symlink/hardlink deployment shows the new content without a redeploy.

Records live in the plugin's ``data.json`` under ``"autopatch"``.  The panel
writes one when it creates an output; outputs without a record are verified
once (patched in a temporary folder and compared) and then adopted.

Nothing here may stop the game from launching: every problem is logged and the
existing output is kept.  Only a user cancellation propagates.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from . import tools
from .fsutil import ci_resolve, glob_files
from .patcher import Patcher, PatchOptions, Target, find_patches, mod_provides, patch_targets

LogFn = Callable[[str], None]
PREFIX = "Dynamic Interface Patcher: "
OUTPUT_SUFFIX = " (DIP Output)"
DATA_KEY = "autopatch"


# --------------------------------------------------------------------------- #
# Profile and staging
# --------------------------------------------------------------------------- #
def read_modlist(profile_dir: Path) -> list[tuple[str, bool]]:
    """``modlist.txt`` entries (name, enabled), highest priority first, separators skipped."""
    out = []
    try:
        lines = (Path(profile_dir) / "modlist.txt").read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return out
    for line in lines:
        if len(line) < 2 or line[0] not in "+-*":
            continue
        name = line[1:]
        if name.endswith("_separator") or name.startswith(("@group:", "__")):
            continue
        out.append((name, line[0] == "+"))
    return out


def mods_dir_for(context: dict) -> Optional[Path]:
    profile_dir = Path(context["profile_dir"])
    for cand in (profile_dir / "mods", profile_dir.parent.parent / "mods",
                 Path(context.get("overwrite_path") or "").parent / "mods"):
        if cand.is_dir():
            return cand
    return None


# --------------------------------------------------------------------------- #
# Fingerprints
# --------------------------------------------------------------------------- #
def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def output_hashes(folder: Path) -> dict[str, str]:
    """Hashes of an output mod's files (paths relative, '/'-separated, meta.ini excluded)."""
    out = {}
    if not folder.is_dir():
        return out
    for p in sorted(folder.rglob("*")):
        if p.is_file() and p.name.lower() != "meta.ini":
            out[p.relative_to(folder).as_posix()] = file_hash(p)
    return out


def _original_bytes(original: Path, t: Target) -> Optional[bytes]:
    from .bsa import BSAArchive
    if t.bsa:
        bsa = ci_resolve(original, t.bsa)
        if bsa is None or not bsa.is_file():
            return None
        arc = BSAArchive(bsa)
        return arc.read(str(t.swf)) if arc.find(str(t.swf)) else None
    f = ci_resolve(original, t.swf)
    return f.read_bytes() if f is not None and f.is_file() else None


def inputs_fingerprint(patch_root: Path, targets: list[Target], original: Path) -> str:
    """Everything the output depends on: patch/shape files and the original SWFs."""
    h = hashlib.sha256()
    for sub in ("Patch", "Shapes"):
        d = ci_resolve(patch_root, sub)
        if d is not None and d.is_dir():
            for f in glob_files(d, "*"):
                h.update(f"{sub}/{f.relative_to(d).as_posix().lower()}\0{file_hash(f)}\0".encode())
    for t in sorted(targets, key=lambda t: t.describe().lower()):
        data = _original_bytes(original, t)
        h.update(f"{t.describe().lower()}\0{_sha(data) if data is not None else 'missing'}\0".encode())
    return h.hexdigest()


# --------------------------------------------------------------------------- #
# Records (shared with the panel)
# --------------------------------------------------------------------------- #
def record_key(game_id: str, output_name: str) -> str:
    return f"{game_id}/{output_name}"


def make_record(patch_mod: Path, patch_root: Path, original: Path, output_dir: Path,
                repack_bsa: bool, fingerprint: Optional[str] = None) -> dict:
    targets = patch_targets(patch_root)
    return {
        "patch_mod": patch_mod.name,
        "patch_rel": os.path.relpath(patch_root, patch_mod),
        "original": str(original),
        "original_mod": original.name,
        "repack_bsa": bool(repack_bsa),
        "inputs": fingerprint or inputs_fingerprint(patch_root, targets, original),
        "files": output_hashes(output_dir),
        "updated": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }


def load_json(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_json_atomic(path: Path, data: dict) -> None:
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def store_record(data_path: Path, key: str, record: dict) -> None:
    """Used by the panel after it creates or replaces an output mod."""
    data = load_json(data_path)
    data.setdefault(DATA_KEY, {})[key] = record
    save_json_atomic(data_path, data)


# --------------------------------------------------------------------------- #
# Writing updated outputs
# --------------------------------------------------------------------------- #
def write_in_place(dest: Path, data: bytes) -> None:
    """Overwrite a file keeping its inode, so hardlinked or symlinked deployments follow."""
    with open(dest, "r+b") as f:
        f.seek(0)
        f.write(data)
        f.truncate()


def _deployed_note(data_root: Path, rel: str, staged: Path, data: bytes) -> str:
    """Make sure the deployed copy of ``rel`` shows the new content; return a problem or ''."""
    dep = ci_resolve(data_root, rel)
    if dep is None or not os.path.lexists(dep):
        return f"{rel} is not deployed"
    try:
        if dep.is_symlink() or os.path.samefile(dep, staged):
            return ""
    except OSError:
        return f"{rel}: could not inspect the deployed file"
    write_in_place(dep, data)  # copy deployment: update the deployed copy as well
    return ""


# --------------------------------------------------------------------------- #
# The launch step
# --------------------------------------------------------------------------- #
class _Cancel(threading.Event):
    """Patcher cancel flag backed by Amethyst's check_cancelled()."""

    def __init__(self, check: Callable[[], None]):
        super().__init__()
        self._check = check

    def is_set(self) -> bool:  # noqa: D401
        try:
            self._check()
        except BaseException:
            return True
        return False


def before_launch(context: dict) -> None:
    log: LogFn = context.get("log") or print
    settings = context.get("settings") or {}
    if not settings.get("auto_patch_on_launch", True):
        return None
    game_id = str(context.get("game_id") or "")
    if "skyrim" not in game_id and "enderal" not in game_id:
        return None
    if not context.get("is_game", True):
        return None  # a tool, not the game
    check = context.get("check_cancelled") or (lambda: None)
    try:
        AutoPatch(context, log, check).run()
    except tools.Cancelled:
        raise
    except Exception as e:  # noqa: BLE001 - never block the launch...
        check()  # ...unless the user cancelled it: re-raises Amethyst's own cancellation
        log(f"{PREFIX}automatic check skipped ({e!r}).")
    return None


class AutoPatch:
    def __init__(self, context: dict, log: LogFn, check: Callable[[], None]):
        self.ctx = context
        self.log = log
        self.check = check
        self.settings = context.get("settings") or {}
        self.game_id = str(context.get("game_id") or "")
        self.data_root = Path(context.get("data_root") or context.get("game_path") or ".")
        self.mods_dir = mods_dir_for(context)
        self._load = context.get("load_data")
        self._save = context.get("save_data")
        self.data_path = Path(context["data_path"]) if context.get("data_path") else None

    # -- data.json ----------------------------------------------------------- #
    def _read_data(self) -> dict:
        if callable(self._load):
            data = self._load()
            return data if isinstance(data, dict) else {}
        return load_json(self.data_path) if self.data_path else {}

    def _write_data(self, data: dict) -> None:
        if callable(self._save):
            self._save(data)
        elif self.data_path:
            save_json_atomic(self.data_path, data)

    # -- main ------------------------------------------------------------------ #
    def run(self) -> None:
        if self.mods_dir is None:
            self.log(f"{PREFIX}could not find the mods folder; automatic check skipped.")
            return
        order = read_modlist(Path(self.ctx["profile_dir"]))
        enabled = [n for n, on in order if on and (self.mods_dir / n).is_dir()]
        self._warn_automatic_patcher(enabled)
        data = self._read_data()
        records: dict = data.setdefault(DATA_KEY, {})
        outputs = [n for n in enabled if n.endswith(OUTPUT_SUFFIX)]
        results = []
        for name in outputs:
            self.check()
            try:
                results.append(self._handle(name, enabled, records))
            except tools.Cancelled:
                raise
            except Exception as e:  # noqa: BLE001
                self.check()  # a cancelled launch must not be reported as an ordinary failure
                results.append(f"'{name}': check failed ({e}); kept the existing output")
        self._note_unpatched(enabled, outputs)
        self._write_data(data)
        changed = [r for r in results if r != "up to date"]
        if outputs and not changed:
            self.log(f"{PREFIX}{len(outputs)} DIP output(s) up to date.")
        for r in changed:
            self.log(PREFIX + r)

    def _handle(self, name: str, enabled: list[str], records: dict) -> str:
        key = record_key(self.game_id, name)
        rec = records.get(key) or {}
        out_dir = self.mods_dir / name
        patch_mod = self.mods_dir / (rec.get("patch_mod") or name[: -len(OUTPUT_SUFFIX)])
        if not patch_mod.is_dir():
            return f"'{name}': its patch mod '{patch_mod.name}' is not installed; skipped"
        root = patch_mod / rec["patch_rel"] if rec.get("patch_rel") else None
        if root is None or not (root / "Patch").exists() and ci_resolve(root, "Patch") is None:
            roots = find_patches(patch_mod)
            if len(roots) != 1:
                return f"'{name}': found {len(roots)} patches in '{patch_mod.name}'; open the plugin to patch it"
            root = roots[0]
        targets = patch_targets(root)
        original = self._original(rec, enabled, targets, patch_mod, out_dir)
        if original is None:
            return f"'{name}': no enabled mod provides the files it patches; skipped"

        inputs = inputs_fingerprint(root, targets, original)
        current = output_hashes(out_dir)
        if rec.get("inputs") == inputs and rec.get("files") == current and rec.get("original") == str(original):
            return "up to date"
        reasons = []
        if not rec:
            reasons.append("first check")
        else:
            if rec.get("original") != str(original):
                reasons.append(f"original mod is now '{original.name}'")
            elif rec.get("inputs") != inputs:
                reasons.append("the patch or the original mod changed")
            if rec.get("files") != current:
                reasons.append("output files were modified")

        self.check()
        new_dir = Path(tempfile.mkdtemp(prefix="DIP_auto_"))
        try:
            opts = PatchOptions(
                output_folder=new_dir, repack_bsa=bool(rec.get("repack_bsa")), backup_existing=False,
                java=str(self.settings.get("java_path") or ""), ffdec_jar=str(self.settings.get("ffdec_jar") or ""),
                ffdec_hints=self._ffdec_hints(),
                ffdec_version=str(self.settings.get("ffdec_version") or tools.FFDEC_VERSION),
                ffdec_fallback=bool(self.settings.get("ffdec_fallback", True)),
                allow_download=bool(self.settings.get("allow_download", True)),
                java_memory=str(self.settings.get("java_memory") or "1024m"))
            Patcher(opts, log=lambda m: None, cancel=_Cancel(self.check)).patch(root, original)
            fresh = output_hashes(new_dir)
            if fresh == current:
                records[self._key(name)] = make_record(patch_mod, root, original, out_dir,
                                                       bool(rec.get("repack_bsa")), inputs)
                return "up to date" if rec else f"'{name}': verified and now tracked"
            if set(fresh) != set(current):
                return (f"'{name}' needs a new patch ({', '.join(reasons)}) but its file list changed; "
                        "open Plugins → Dynamic Interface Patcher and patch it again")
            problems = []
            for rel in sorted(fresh):
                if fresh[rel] == current[rel]:
                    continue
                data = (new_dir / rel).read_bytes()
                staged = out_dir / rel
                write_in_place(staged, data)
                note = _deployed_note(self.data_root, rel, staged, data)
                if note:
                    problems.append(note)
            records[self._key(name)] = make_record(patch_mod, root, original, out_dir,
                                                   bool(rec.get("repack_bsa")), inputs)
            msg = f"re-patched '{name}' ({', '.join(reasons)})"
            if problems:
                msg += "; deploy again to use it: " + "; ".join(problems)
            return msg
        finally:
            shutil.rmtree(new_dir, ignore_errors=True)

    def _key(self, name: str) -> str:
        return record_key(self.game_id, name)

    def _original(self, rec: dict, enabled: list[str], targets: list[Target],
                  patch_mod: Path, out_dir: Path) -> Optional[Path]:
        required = [t for t in targets if t.required] or targets
        recorded = Path(rec["original"]) if rec.get("original") else None
        # keep the original chosen in the panel while it is still usable: a folder outside the
        # mods folder, or a mod that is still enabled in this profile
        if recorded and recorded.is_dir() and (recorded.parent != self.mods_dir or recorded.name in enabled) \
                and all(mod_provides(recorded, t) for t in required):
            return recorded
        for n in enabled:  # highest priority first: the mod whose files the game uses
            p = self.mods_dir / n
            if p in (patch_mod, out_dir) or n.endswith(OUTPUT_SUFFIX):
                continue
            if all(mod_provides(p, t) for t in required):
                return p
        return None

    def _ffdec_hints(self) -> list[Path]:
        hints = []
        try:
            for e in sorted(os.scandir(self.mods_dir), key=lambda e: e.name):
                low = e.name.lower()
                if e.is_dir() and ("dip" in low or "interface patcher" in low):
                    jar = tools.windows_dip_ffdec(Path(e.path))
                    if jar:
                        hints.append(jar)
        except OSError:
            pass
        return hints

    def _note_unpatched(self, enabled: list[str], outputs: list[str]) -> None:
        have = {o[: -len(OUTPUT_SUFFIX)] for o in outputs}
        missing = []
        for n in enabled:
            if n in have or n.endswith(OUTPUT_SUFFIX) or "dip" not in n.lower():
                continue
            if find_patches(self.mods_dir / n, max_depth=2):
                missing.append(n)
        if missing:
            self.log(f"{PREFIX}enabled DIP patch(es) without an output mod: {', '.join(missing)}. "
                     "Open Plugins → Dynamic Interface Patcher to create them.")

    def _warn_automatic_patcher(self, enabled: list[str]) -> None:
        for n in enabled:
            if ci_resolve(self.mods_dir / n, "SKSE/Plugins/AutomaticPatcher.dll"):
                label = f"'{n}'" if "automatic patcher" in n.lower() else f"'{n}' (Automatic Patcher)"
                self.log(f"{PREFIX}{label} is enabled. It runs the Windows DIP.exe at "
                         "game start and writes into the game folder behind Amethyst; disable it and let "
                         "this plugin keep the DIP outputs up to date.")
                return
