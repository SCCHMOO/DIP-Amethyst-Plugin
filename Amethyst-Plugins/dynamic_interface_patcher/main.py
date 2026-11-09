"""
Dynamic Interface Patcher - Amethyst Mod Manager plugin (ui: qt).

Native Linux port of Cutleast's DIP: finds DIP patches in the selected
profile, patches the original mod's SWFs without Wine, and installs the result
as a new mod right above the patch.  All work runs on worker threads; widgets
are only touched from the GUI thread via signals.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
import zipfile
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QObject, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QFileDialog, QGridLayout, QGroupBox, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from amethyst_rpc import Headless, RpcError  # noqa: E402
from dip_core import DIP_VERSION, __version__, tools  # noqa: E402
from dip_core import autopatch  # noqa: E402
from dip_core.patcher import DipError, Patcher, PatchOptions, resolve_patch_root  # noqa: E402
from library import FoundPatch, Library, ModInfo, safe_mod_name  # noqa: E402

TITLE = "Dynamic Interface Patcher"


class _Bridge(QObject):
    log = Signal(str)
    scanned = Signal(object)        # (list[FoundPatch], list[ModInfo], str staging)
    done = Signal(bool, str)        # changed, summary
    closed = Signal()


class DipPanel(QWidget):
    preferred_size = QSize(1100, 820)

    def __init__(self, context: dict, parent: Optional[QWidget], on_close: Callable[[bool], None]):
        super().__init__(parent)
        self._ctx = context
        self._on_close = on_close
        self._settings = context.get("settings") or {}
        self._busy = False
        self._changed = False
        self._close_after = False
        self._started = False
        self._cancel = threading.Event()
        self._rpc: Optional[Headless] = None
        self._rpc_lock = threading.Lock()
        self._patches: list[FoundPatch] = []
        self._mods: list[ModInfo] = []
        self._hints: list[Path] = []
        self._b = _Bridge()
        self._b.log.connect(self._append_log)
        self._b.scanned.connect(self._fill)
        self._b.done.connect(self._on_done)
        self._b.closed.connect(self._closed)

        root = QVBoxLayout(self)
        intro = QLabel(
            f"Native Linux port of <b>DIP {DIP_VERSION}</b> by Cutleast &ndash; no Wine needed. "
            "Each checked patch is applied to its original mod and the patched SWF files are "
            "installed as a new mod placed just above the patch. Deploy afterwards.")
        intro.setWordWrap(True)
        root.addWidget(intro)
        self._tools = QLabel()
        self._tools.setWordWrap(True)
        self._tools.setTextInteractionFlags(Qt.TextSelectableByMouse)
        root.addWidget(self._tools)

        self._tree = QTreeWidget()
        self._tree.setHeaderLabels(["Patch", "Original mod", "Output mod", "Status"])
        self._tree.setRootIsDecorated(False)
        self._tree.setSelectionMode(QAbstractItemView.NoSelection)
        hdr = self._tree.header()
        hdr.setSectionResizeMode(0, QHeaderView.Interactive)
        hdr.setStretchLastSection(True)
        self._tree.setColumnWidth(0, 300)
        self._tree.setColumnWidth(1, 270)
        self._tree.setColumnWidth(2, 300)
        root.addWidget(self._tree, 3)

        opts = QHBoxLayout()
        self._repack = QCheckBox("Repack BSA instead of loose files (experimental)")
        self._repack.setChecked(bool(self._s("repack_bsa", False)))
        self._debug = QCheckBox("Write debug files")
        opts.addWidget(self._repack)
        opts.addWidget(self._debug)
        opts.addStretch(1)
        self._rescan_btn = QPushButton("Rescan")
        self._rescan_btn.clicked.connect(self._scan)
        self._patch_btn = QPushButton("Patch checked")
        self._patch_btn.setDefault(True)
        self._patch_btn.clicked.connect(self._patch_checked)
        opts.addWidget(self._rescan_btn)
        opts.addWidget(self._patch_btn)
        root.addLayout(opts)

        manual = QGroupBox("Manual (any folders, like DIP.exe)")
        grid = QGridLayout(manual)
        self._m_patch = QLineEdit()
        self._m_orig = QLineEdit()
        self._m_out = QLineEdit()
        for row, (label, edit, title) in enumerate((
                ("Patch folder:", self._m_patch, "Select the DIP patch folder (contains 'Patch')"),
                ("Original mod:", self._m_orig, "Select the original mod folder"),
                ("Output folder:", self._m_out, "Select the output folder"))):
            grid.addWidget(QLabel(label), row, 0)
            grid.addWidget(edit, row, 1)
            b = QPushButton("Browse…")
            b.clicked.connect(lambda _=False, e=edit, t=title: self._browse(e, t))
            grid.addWidget(b, row, 2)
        self._manual_btn = QPushButton("Patch to folder")
        self._manual_btn.clicked.connect(self._patch_manual)
        grid.addWidget(self._manual_btn, 2, 3)
        root.addWidget(manual)

        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        root.addWidget(self._log, 2)

        foot = QHBoxLayout()
        cp = QPushButton("Copy log")
        cp.clicked.connect(lambda: QGuiApplication.clipboard().setText(self._log.toPlainText()))
        cl = QPushButton("Clear log")
        cl.clicked.connect(self._log.clear)
        self._close_btn = QPushButton("Close")
        self._close_btn.clicked.connect(self.request_close)
        foot.addWidget(cp)
        foot.addWidget(cl)
        foot.addStretch(1)
        foot.addWidget(self._close_btn)
        root.addLayout(foot)

        self._refresh_tools_label()
        gid = str(context.get("game_id", ""))
        if gid and "skyrim" not in gid and "enderal" not in gid:
            self._append_log(f"Note: DIP targets Skyrim UI mods; the selected game is {context.get('game', '?')}.")

    # -- lifecycle ---------------------------------------------------------- #
    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if not self._started:
            self._started = True
            QTimer.singleShot(0, self._scan)

    def request_close(self) -> None:
        if self._busy:
            self._cancel.set()
            self._close_after = True
            self._append_log("Stopping… the panel closes when the current step finishes.")
            return
        self._finish()

    def _finish(self) -> None:
        self._set_busy(True)

        def work() -> None:
            with self._rpc_lock:
                if self._rpc is not None:
                    self._rpc.close()
                    self._rpc = None
            self._b.closed.emit()

        threading.Thread(target=work, daemon=True).start()

    def _closed(self) -> None:
        cb, self._on_close = self._on_close, (lambda _c: None)
        cb(self._changed)

    # -- helpers ------------------------------------------------------------ #
    def _s(self, key: str, default=None):
        v = self._settings.get(key, default)
        return default if v is None else v

    def _append_log(self, msg: str) -> None:
        self._log.appendPlainText(msg)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        for w in (self._patch_btn, self._rescan_btn, self._manual_btn):
            w.setEnabled(not busy)

    def _browse(self, edit: QLineEdit, title: str) -> None:
        p = QFileDialog.getExistingDirectory(self, title, edit.text() or str(Path.home()))
        if p:
            edit.setText(p)

    def _refresh_tools_label(self) -> None:
        java = tools.find_java(str(self._s("java_path", "")))
        custom = str(self._s("ffdec_jar", ""))
        want = self._ffdec_version()
        jar = tools.find_ffdec_jar(custom, self._hints, want)
        xd = tools.find_xdelta3()
        parts = [f"Java: {java}" if java else "<b>Java not found</b> (only needed for JSON patches)"]
        if jar:
            parts.append(f"FFDec {tools.jar_version(jar) or '?'}: {jar}")
        elif custom.lower().endswith(".zip"):
            parts.append(f"FFDec will be installed from {custom}")
        elif self._s("allow_download", True):
            parts.append(f"FFDec {want} will be downloaded when a JSON patch needs it")
        else:
            others = tools.ffdec_candidates(self._hints)
            parts.append(f"<b>FFDec {want} not found</b>" + (
                f"; would fall back to {tools.jar_version(others[0]) or '?'}" if others else ""))
        parts.append("xdelta: built-in" + (f" (+ {xd})" if xd else ""))
        self._tools.setText("  |  ".join(parts))

    def _ffdec_version(self) -> str:
        return str(self._s("ffdec_version", tools.FFDEC_VERSION) or tools.FFDEC_VERSION).strip()

    def _backend(self) -> Headless:
        with self._rpc_lock:
            if self._rpc is None:
                cmd = self._ctx.get("headless_command")
                if not cmd:
                    raise RpcError("No Amethyst backend available (use the manual section).")
                rpc = Headless(list(cmd), "dynamic_interface_patcher", __version__)
                rpc.start()
                self._rpc = rpc
            return self._rpc

    def _target(self) -> dict:
        return {"game": self._ctx.get("game_id"), "profile": self._ctx.get("profile")}

    def _options(self, out: Path, backup: bool) -> PatchOptions:
        return PatchOptions(
            output_folder=out, repack_bsa=self._repack.isChecked(), debug=self._debug.isChecked(),
            debug_folder=tools.DATA_DIR / "debug", backup_existing=backup,
            java=str(self._s("java_path", "")), ffdec_jar=str(self._s("ffdec_jar", "")),
            ffdec_hints=self._hints, ffdec_version=self._ffdec_version(),
            ffdec_fallback=bool(self._s("ffdec_fallback", True)),
            allow_download=bool(self._s("allow_download", True)),
            java_memory=str(self._s("java_memory", "1024m") or "1024m"))

    # -- scanning ----------------------------------------------------------- #
    def _scan(self) -> None:
        if self._busy:
            return
        if not self._ctx.get("headless_command") or not self._ctx.get("game_id") or not self._ctx.get("profile"):
            self._append_log("No game/profile selected - only the manual section is available.")
            return
        self._set_busy(True)
        self._append_log("Scanning the profile's mods for DIP patches…")

        def work() -> None:
            result = ([], [], "")
            try:
                listing = self._backend().call("mods.list", self._target())
                mods = ModInfo.from_list(listing.get("mods", []))
                lib = Library(mods, self._b.log.emit)
                result = (lib.scan(self._cancel.is_set), mods, listing.get("staging_path", ""), lib.ffdec_hints())
            except Exception as e:  # noqa: BLE001
                self._b.log.emit(f"Scan failed: {e}")
            self._b.scanned.emit(result)

        threading.Thread(target=work, daemon=True).start()

    def _fill(self, result) -> None:
        patches, mods, staging = result[0], result[1], result[2]
        if len(result) > 3:
            self._hints = result[3]
        self._patches, self._mods = patches, mods
        self._refresh_tools_label()
        names = {m.name for m in mods}
        self._tree.clear()
        for fp in patches:
            it = QTreeWidgetItem([fp.label, "", "", ""])
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setToolTip(0, f"{fp.root}\n" + "\n".join(
                f"{t.kind}: {t.describe()}{'' if t.required else ' (optional)'}" for t in fp.targets))
            self._tree.addTopLevelItem(it)
            combo = QComboBox()
            for c in fp.candidates:
                combo.addItem(c.name + ("" if c.enabled else "  (disabled)"), c.name)
            combo.addItem("Choose folder…", "__browse__")
            combo.activated.connect(lambda _i, cb=combo: self._combo_changed(cb))
            self._tree.setItemWidget(it, 1, combo)
            out = QLineEdit(fp.default_output_name)
            self._tree.setItemWidget(it, 2, out)
            ok = bool(fp.candidates)
            status = "Ready" if ok else "Original mod not found - choose a folder"
            if fp.default_output_name in names:
                status += " · output mod exists (will be replaced)"
            it.setText(3, status)
            it.setCheckState(0, Qt.Checked if ok and fp.mod.enabled else Qt.Unchecked)
        self._append_log(f"Found {len(patches)} DIP patch(es)" + (f" in {staging}" if staging else "") + ".")
        if patches and not any(fp.mod.enabled for fp in patches):
            self._append_log("None of the patch mods are enabled; check the ones you want to apply.")
        self._set_busy(False)
        if self._close_after:
            self._finish()

    def _combo_changed(self, combo: QComboBox) -> None:
        if combo.currentData() != "__browse__":
            return
        p = QFileDialog.getExistingDirectory(self, "Select the original mod folder", str(Path.home()))
        if p:
            combo.insertItem(combo.count() - 1, p, p)
            combo.setCurrentIndex(combo.count() - 2)
        else:
            combo.setCurrentIndex(0)

    # -- patching ----------------------------------------------------------- #
    def _patch_checked(self) -> None:
        if self._busy:
            return
        jobs = []
        existing = {m.name for m in self._mods}
        for i in range(self._tree.topLevelItemCount()):
            it = self._tree.topLevelItem(i)
            if it.checkState(0) != Qt.Checked:
                continue
            fp = self._patches[i]
            combo: QComboBox = self._tree.itemWidget(it, 1)
            out: QLineEdit = self._tree.itemWidget(it, 2)
            data = combo.currentData()
            if not data or data == "__browse__":
                QMessageBox.warning(self, TITLE, f"Choose the original mod for '{fp.label}'.")
                return
            orig = next((m for m in self._mods if m.name == data), None)
            name = safe_mod_name(out.text())
            jobs.append((fp, orig, Path(orig.path) if orig else Path(data), name))
        if not jobs:
            QMessageBox.information(self, TITLE, "Check at least one patch first.")
            return
        names = [j[3] for j in jobs]
        if len(set(names)) != len(names):
            QMessageBox.warning(self, TITLE, "Two patches use the same output mod name.")
            return
        replace = [n for n in names if n in existing]
        if replace and QMessageBox.question(
                self, TITLE, "These output mods already exist and their files will be replaced:\n\n"
                + "\n".join(replace) + "\n\nContinue?") != QMessageBox.Yes:
            return
        self._cancel.clear()
        self._set_busy(True)

        def work() -> None:
            ok = failed = 0
            for fp, orig, orig_path, name in jobs:
                if self._cancel.is_set():
                    break
                self._b.log.emit(f"\n=== {fp.label} → {name}")
                tmp = Path(tempfile.mkdtemp(prefix="DIP_out_"))
                try:
                    Patcher(self._options(tmp, backup=False), self._b.log.emit, self._cancel).patch(fp.root, orig_path)
                    self._install(fp, orig, name, tmp, orig_path)
                    ok += 1
                except Exception as e:  # noqa: BLE001
                    failed += 1
                    self._b.log.emit(f"Failed: {e}")
                finally:
                    shutil.rmtree(tmp, ignore_errors=True)
            summary = f"Done: {ok} patched, {failed} failed."
            if ok:
                summary += " Deploy in Amethyst so the game sees the patched files."
            self._b.done.emit(ok > 0, summary)

        threading.Thread(target=work, daemon=True).start()

    def _install(self, fp: FoundPatch, orig: Optional[ModInfo], name: str, src: Path, orig_path: Path) -> None:
        rpc = self._backend()
        listing = rpc.call("mods.list", self._target())
        row = next((r for r in listing.get("mods", []) if r.get("name") == name and r.get("kind") == "mod"), None)
        staging = Path(listing.get("staging_path", ""))
        if row is None:
            anchors = [m for m in (fp.mod, orig) if m is not None]
            anchor = max(anchors, key=lambda m: m.priority).name
            try:
                res = rpc.run_job("mods.create.start", {**self._target(), "name": name,
                                                        "revision": listing.get("group_revision"),
                                                        "position": {"before": anchor}, "enabled": False})
            except RpcError as e:
                suggested = (e.data or {}).get("suggested") if isinstance(e.data, dict) else None
                if e.code != "invalid_mod_name" or not suggested:
                    raise
                name = suggested
                listing = rpc.call("mods.list", self._target())
                res = rpc.run_job("mods.create.start", {**self._target(), "name": name,
                                                        "revision": listing.get("group_revision"),
                                                        "position": {"before": anchor}, "enabled": False})
            name = ((res or {}).get("mod") or {}).get("name") or name
            self._b.log.emit(f"Created mod '{name}' above '{anchor}'.")
        else:
            self._b.log.emit(f"Replacing the files of existing mod '{name}'.")

        # Install the output through Amethyst's normal archive installer, replacing the
        # (new or previous) mod in place.  Unlike copying files into the folder, this
        # updates Amethyst's file index (Filegraph), which deploy relies on.
        work = Path(tempfile.mkdtemp(prefix="DIP_zip_"))
        archive = work / f"{name}.zip"  # recorded as installationfile in meta.ini
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
            for p in sorted(src.rglob("*")):
                if p.is_file():
                    z.write(p, p.relative_to(src).as_posix())
        try:
            res = rpc.run_job("downloads.install.start", {
                **self._target(), "archive_path": str(archive), "name": name,
                "if_exists": "replace", "installer_policy": "reject"})
        finally:
            shutil.rmtree(work, ignore_errors=True)
        self._changed = True
        mod = (res or {}).get("mod") or {}
        if not mod.get("enabled"):
            rpc.run_job("mods.set_enabled.start", {**self._target(), "changes": [{"name": name, "enabled": True}]})
        dest = Path(mod.get("path") or staging / name)
        files = [p.relative_to(dest) for p in dest.rglob("*") if p.is_file() and p.name.lower() != "meta.ini"]
        self._b.log.emit(f"Installed {len(files)} file(s) into '{name}' (enabled): "
                         + ", ".join(str(f) for f in files))
        try:  # remember how this output was made, for the automatic check before launch
            if name.endswith(autopatch.OUTPUT_SUFFIX):
                rec = autopatch.make_record(Path(fp.mod.path), Path(fp.root), Path(orig_path), dest,
                                            self._repack.isChecked())
                autopatch.store_record(self._data_path(), autopatch.record_key(str(self._ctx.get("game_id")), name), rec)
            else:
                self._b.log.emit("Note: the automatic check before launch only covers output mods named "
                                 f"'<patch mod>{autopatch.OUTPUT_SUFFIX}'.")
        except Exception as e:  # noqa: BLE001 - bookkeeping only
            self._b.log.emit(f"Could not record this output for the automatic check: {e}")

    def _data_path(self) -> Path:
        return Path(self._ctx.get("data_path") or Path(__file__).resolve().parent / "data.json")

    def _patch_manual(self) -> None:
        if self._busy:
            return
        patch, orig, out = (Path(e.text().strip()).expanduser() for e in (self._m_patch, self._m_orig, self._m_out))
        if not all(e.text().strip() for e in (self._m_patch, self._m_orig, self._m_out)):
            QMessageBox.information(self, TITLE, "Fill in the patch, original mod and output folders.")
            return
        try:
            root = resolve_patch_root(patch)
        except DipError as e:
            QMessageBox.warning(self, TITLE, str(e))
            return
        self._cancel.clear()
        self._set_busy(True)

        def work() -> None:
            try:
                res = Patcher(self._options(out, backup=True), self._b.log.emit, self._cancel).patch(root, orig)
                self._b.done.emit(False, f"Done: {len(res.written)} file(s) written to {out}.")
            except Exception as e:  # noqa: BLE001
                self._b.done.emit(False, f"Failed: {e}")

        threading.Thread(target=work, daemon=True).start()

    def _on_done(self, changed: bool, summary: str) -> None:
        self._changed = self._changed or changed
        self._append_log(summary)
        self._set_busy(False)
        if self._close_after:
            self._finish()
        elif changed:
            self._scan()


def create_view(context: dict, parent: QWidget, on_close: Callable[[bool], None]) -> QWidget:
    return DipPanel(context, parent, on_close)
