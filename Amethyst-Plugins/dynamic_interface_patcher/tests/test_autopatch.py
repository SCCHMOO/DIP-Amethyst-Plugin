"""
Tests for the before-launch auto-patcher, on a miniature Amethyst setup:

    staging/mods/RaceMenu/RaceMenu.bsa                      original mod
    staging/mods/X DIP/X DIP/Patch/RaceMenu.bsa/interface/racesex_menu.bin
    staging/mods/X DIP (DIP Output)/interface/racesex_menu.swf
    staging/profiles/P/modlist.txt
    game/Data/interface/racesex_menu.swf                    deployed (symlink or copy)
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dip_core import autopatch, bsa  # noqa: E402
from dip_core.patcher import Patcher, PatchOptions  # noqa: E402

XDELTA3 = shutil.which("xdelta3")
OUT = "X DIP (DIP Output)"
REL = "interface/racesex_menu.swf"


class CancelledByUser(Exception):
    pass


@unittest.skipUnless(XDELTA3, "xdelta3 not installed")
class AutoPatchTests(unittest.TestCase):
    def setUp(self):
        self.t = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.t)
        self.mods = self.t / "staging/mods"
        self.profile = self.t / "staging/profiles/P"
        self.data_root = self.t / "game/Data"
        for d in (self.mods, self.profile, self.data_root):
            d.mkdir(parents=True)
        self.orig_swf = os.urandom(30_000)
        (self.t / "src.swf").write_bytes(self.orig_swf)
        (self.mods / "RaceMenu").mkdir()
        bsa.write_archive({REL: self.t / "src.swf"}, self.mods / "RaceMenu/RaceMenu.bsa")
        self.patch_dir = self.mods / "X DIP/X DIP/Patch/RaceMenu.bsa/interface"
        self.patch_dir.mkdir(parents=True)
        self.make_patch(b"v1")
        (self.mods / OUT).mkdir()
        (self.mods / OUT / "meta.ini").write_text("[General]\n")
        self.output = self.mods / OUT / REL
        self.output.parent.mkdir(parents=True)
        self.output.write_bytes(self.expected())
        self.write_modlist(["+" + OUT, "+X DIP", "+RaceMenu"])
        self.deploy("symlink")
        self.store: dict = {}
        self.logs: list[str] = []

    # -- fixtures ------------------------------------------------------------ #
    def make_patch(self, marker: bytes) -> None:
        new = self.orig_swf[:1000] + marker * 50 + self.orig_swf[1000:]
        (self.t / "new.swf").write_bytes(new)
        subprocess.run([XDELTA3, "-e", "-f", "-S", "lzma", "-s", str(self.t / "src.swf"), str(self.t / "new.swf"),
                        str(self.patch_dir / "racesex_menu.bin")], check=True)
        self.current_target = new

    def expected(self) -> bytes:
        return self.current_target

    def write_modlist(self, lines: list[str]) -> None:
        (self.profile / "modlist.txt").write_text("\n".join(["-Some_separator", *lines]) + "\n")

    def deploy(self, mode: str) -> None:
        dep = self.data_root / REL
        if dep.exists() or dep.is_symlink():
            dep.unlink()
        dep.parent.mkdir(parents=True, exist_ok=True)
        if mode == "symlink":
            dep.symlink_to(self.output)
        else:
            shutil.copyfile(self.output, dep)

    def context(self, **over) -> dict:
        ctx = {"api_version": 1, "game_id": "skyrim_se", "profile": "P", "profile_dir": str(self.profile),
               "data_root": str(self.data_root), "game_path": str(self.data_root.parent), "is_game": True,
               "overwrite_path": str(self.t / "staging/overwrite"), "settings": {}, "log": self.logs.append,
               "check_cancelled": lambda: None, "load_data": lambda: dict(self.store),
               "save_data": lambda d: self.store.update(d)}
        ctx.update(over)
        return ctx

    def launch(self, **over) -> None:
        self.logs.clear()
        autopatch.before_launch(self.context(**over))

    def record(self) -> dict:
        return self.store.get(autopatch.DATA_KEY, {}).get(autopatch.record_key("skyrim_se", OUT), {})

    # -- tests ----------------------------------------------------------------- #
    def test_first_launch_adopts_correct_output_without_writing(self):
        ino, mtime = self.output.stat().st_ino, self.output.stat().st_mtime_ns
        self.launch()
        self.assertTrue(any("verified and now tracked" in m for m in self.logs), self.logs)
        self.assertEqual((self.output.stat().st_ino, self.output.stat().st_mtime_ns), (ino, mtime))
        self.assertEqual(self.record()["original_mod"], "RaceMenu")

    def test_unchanged_second_launch_does_not_patch(self):
        self.launch()
        original_patch = Patcher.patch
        Patcher.patch = lambda *a, **k: (_ for _ in ()).throw(AssertionError("patched again"))
        try:
            self.launch()
        finally:
            Patcher.patch = original_patch
        self.assertEqual(self.logs, ["Dynamic Interface Patcher: 1 DIP output(s) up to date."])

    def test_changed_patch_repatches_in_place_and_symlink_sees_it(self):
        self.launch()
        ino = self.output.stat().st_ino
        self.make_patch(b"v2")                      # the patch mod was updated
        self.launch()
        self.assertTrue(any("re-patched" in m and "changed" in m for m in self.logs), self.logs)
        self.assertEqual(self.output.read_bytes(), self.expected())
        self.assertEqual(self.output.stat().st_ino, ino)                # rewritten in place
        self.assertEqual((self.data_root / REL).read_bytes(), self.expected())  # deployed view follows

    def test_copy_deployment_is_updated_too(self):
        self.deploy("copy")
        self.launch()
        self.make_patch(b"v3")
        self.launch()
        self.assertEqual((self.data_root / REL).read_bytes(), self.expected())
        self.assertFalse(any("deploy again" in m for m in self.logs), self.logs)

    def test_modified_output_is_restored(self):
        self.launch()
        self.output.write_bytes(b"broken")
        self.launch()
        self.assertTrue(any("output files were modified" in m for m in self.logs), self.logs)
        self.assertEqual(self.output.read_bytes(), self.expected())

    def test_failure_keeps_output_and_does_not_block_launch(self):
        self.launch()
        before = self.output.read_bytes()
        (self.patch_dir / "racesex_menu.bin").write_bytes(b"not a vcdiff file")
        self.launch()  # must not raise
        self.assertTrue(any("kept the existing output" in m for m in self.logs), self.logs)
        self.assertEqual(self.output.read_bytes(), before)

    def test_cancel_propagates(self):
        self.make_patch(b"v4")  # stale, so work is needed

        before = self.output.read_bytes()

        def cancelled():
            raise CancelledByUser()
        with self.assertRaises(CancelledByUser):  # Amethyst's own exception reaches Amethyst
            self.launch(check_cancelled=cancelled)
        self.assertEqual(self.output.read_bytes(), before)

    def test_cancel_during_ffdec_or_patching_still_cancels(self):
        self.launch()
        self.make_patch(b"v7")
        calls = {"n": 0}

        def cancel_later():  # first checks pass, then the user presses Stop
            calls["n"] += 1
            if calls["n"] > 2:
                raise CancelledByUser()
        with self.assertRaises(BaseException) as cm:
            self.launch(check_cancelled=cancel_later)
        self.assertNotIsInstance(cm.exception, AssertionError)

    def test_disabled_tool_launch_and_other_games_do_nothing(self):
        self.make_patch(b"v5")
        before = self.output.read_bytes()
        self.launch(settings={"auto_patch_on_launch": False})
        self.launch(is_game=False)
        self.launch(game_id="fallout4")
        self.assertEqual((self.output.read_bytes(), self.logs), (before, []))

    def test_disabled_output_mod_is_ignored(self):
        self.write_modlist(["-" + OUT, "+X DIP", "+RaceMenu"])
        self.make_patch(b"v6")
        before = self.output.read_bytes()
        self.launch()
        self.assertEqual(self.output.read_bytes(), before)
        self.assertTrue(any("without an output mod" in m for m in self.logs), self.logs)

    def test_panel_record_means_no_first_launch_verification(self):
        rec = autopatch.make_record(self.mods / "X DIP", self.mods / "X DIP/X DIP", self.mods / "RaceMenu",
                                    self.mods / OUT, False)
        self.store[autopatch.DATA_KEY] = {autopatch.record_key("skyrim_se", OUT): rec}
        self.launch()
        self.assertEqual(self.logs, ["Dynamic Interface Patcher: 1 DIP output(s) up to date."])

    def test_newer_original_mod_wins(self):
        self.launch()
        # a second, higher-priority RaceMenu build with the same SWF (e.g. reinstalled under a new name)
        shutil.copytree(self.mods / "RaceMenu", self.mods / "RaceMenu NEW")
        self.write_modlist(["+" + OUT, "+X DIP", "+RaceMenu NEW", "-RaceMenu"])
        self.launch()
        self.assertEqual(self.record()["original_mod"], "RaceMenu NEW")

    def test_warns_about_automatic_patcher(self):
        ap = self.mods / "Automatic Patcher/SKSE/Plugins"
        ap.mkdir(parents=True)
        (ap / "AutomaticPatcher.dll").write_bytes(b"MZ")
        self.write_modlist(["+" + OUT, "+X DIP", "+Automatic Patcher", "+RaceMenu"])
        self.launch()
        self.assertTrue(any("Automatic Patcher" in m for m in self.logs), self.logs)


if __name__ == "__main__":
    unittest.main()
