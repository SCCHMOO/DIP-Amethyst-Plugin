"""
Self-contained tests for dip_core (no game files needed).

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import random
import shutil
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dip_core import bsa, lz4frame, vcdiff  # noqa: E402
from dip_core.fsutil import ci_dest, ci_resolve, glob_files  # noqa: E402
from dip_core.patcher import (DipError, Patcher, PatchOptions, _split_bsa, patch_targets,  # noqa: E402
                              process_patch_data, resolve_patch_root)
from dip_core.xmlutil import load_jsonc, split_frames, unsplit_frames  # noqa: E402

XDELTA3 = shutil.which("xdelta3")


def lz4_compress_literals(data: bytes) -> bytes:
    """A valid (if useless) LZ4 frame: one block holding only literals."""
    n = len(data)
    token_lit = min(n, 15)
    block = bytearray([token_lit << 4])
    if n >= 15:
        rest = n - 15
        while rest >= 255:
            block.append(255)
            rest -= 255
        block.append(rest)
    block += data
    import struct
    return (struct.pack("<I", lz4frame.MAGIC) + bytes([0x40, 0x40, 0xC0])
            + struct.pack("<I", len(block)) + bytes(block) + struct.pack("<I", 0))


class Lz4Tests(unittest.TestCase):
    def test_literal_frame(self):
        data = os.urandom(1000)
        self.assertEqual(lz4frame._decompress_py(lz4_compress_literals(data)), data)

    def test_block_with_overlapping_match(self):
        # literals "ab", then match offset 2 length 10 -> "ab" * 6, then literal "!"
        out = bytearray()
        lz4frame._block(bytes([0x26]) + b"ab" + bytes([2, 0]) + bytes([0x10]) + b"!", out)
        self.assertEqual(bytes(out), b"ab" * 6 + b"!")


class FsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "Interface" / "RaceMenu").mkdir(parents=True)
        (self.tmp / "Interface" / "RaceMenu" / "BottomBar.SWF").write_bytes(b"x")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_ci_resolve(self):
        hit = ci_resolve(self.tmp, "interface\\racemenu\\bottombar.swf")
        self.assertEqual(hit, self.tmp / "Interface" / "RaceMenu" / "BottomBar.SWF")
        self.assertIsNone(ci_resolve(self.tmp, "interface/missing.swf"))

    def test_ci_dest_reuses_existing_case(self):
        self.assertEqual(ci_dest(self.tmp, "interface/racemenu/new.swf"),
                         self.tmp / "Interface" / "RaceMenu" / "new.swf")

    def test_glob_case_insensitive(self):
        self.assertEqual(len(glob_files(self.tmp, "*.swf")), 1)


class VcdiffTests(unittest.TestCase):
    @unittest.skipUnless(XDELTA3, "xdelta3 not installed")
    def test_matches_xdelta3(self):
        rnd = random.Random(1)
        src = bytes(rnd.getrandbits(8) for _ in range(200_000))
        tgt = bytearray(src)
        for _ in range(300):  # sprinkle edits, inserts and deletes
            i = rnd.randrange(len(tgt))
            op = rnd.choice("eid")
            if op == "e":
                tgt[i] = rnd.getrandbits(8)
            elif op == "i":
                tgt[i:i] = os.urandom(rnd.randrange(1, 40))
            else:
                del tgt[i:i + rnd.randrange(1, 40)]
        tgt += b"Z" * 5000  # long run
        with tempfile.TemporaryDirectory() as d:
            s, t = Path(d) / "s", Path(d) / "t"
            s.write_bytes(src)
            t.write_bytes(bytes(tgt))
            for flags in (["-S", "lzma"], ["-S", "none"], ["-S", "lzma", "-A"]):
                p = Path(d) / "p"
                subprocess.run([XDELTA3, "-e", "-f", *flags, "-s", str(s), str(t), str(p)], check=True)
                self.assertEqual(vcdiff.decode(src, p.read_bytes()), bytes(tgt), flags)

    @unittest.skipUnless(XDELTA3, "xdelta3 not installed")
    def test_checksum_detects_wrong_source(self):
        src = os.urandom(50_000)
        tgt = src[:20_000] + b"patched" + src[20_000:]
        with tempfile.TemporaryDirectory() as d:
            s, t, p = (Path(d) / n for n in "stp")
            s.write_bytes(src)
            t.write_bytes(tgt)
            subprocess.run([XDELTA3, "-e", "-f", "-s", str(s), str(t), str(p)], check=True)
            with self.assertRaises(vcdiff.VCDiffError):
                vcdiff.decode(os.urandom(50_000), p.read_bytes())


class BsaTests(unittest.TestCase):
    def test_hash_known_values(self):
        # values read from a real Skyrim SE archive (RaceMenu.bsa, RaceMenu AE)
        self.assertEqual(bsa.tes_hash("interface", True), 0xE1E631CE69096365)
        self.assertEqual(bsa.tes_hash("interface\\racemenu", True), 0xEF94EF3569126E75)
        self.assertEqual(bsa.tes_hash("interface/RaceMenu", True), 0xEF94EF3569126E75)
        self.assertEqual(bsa.tes_hash("racesex_menu.swf"), 0xE1724B68720C6E75)
        self.assertEqual(bsa.tes_hash("bottombar.swf"), 0x45D7386962096172)

    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            files = {}
            for i, name in enumerate(["interface/racesex_menu.swf", "interface/racemenu/bottombar.swf",
                                      "interface/racemenu/buttonart.swf", "textures/a/b.dds"]):
                p = d / f"src{i}"
                p.write_bytes(os.urandom(3000 + i))
                files[name] = p
            for version in (104, 105):
                for compress in (False, True):
                    out = d / f"t{version}{compress}.bsa"
                    bsa.write_archive(files, out, version=version, compress=compress)
                    arc = bsa.BSAArchive(out)
                    self.assertEqual(len(arc.entries), 4)
                    for name, p in files.items():
                        self.assertEqual(arc.read(name.upper()), p.read_bytes())


class PatchDataTests(unittest.TestCase):
    def test_jsonc(self):
        text = '{\n // c\n "a": "x // not a comment", /* b */ "b": [1, 2,],\n}'
        self.assertEqual(load_jsonc(text), {"a": "x // not a comment", "b": [1, 2]})

    def test_process_patch_data(self):
        data = {"displayRect": {"~Xmax": "1"},
                "tags": [{"#type": "DefineSpriteTag", "#spriteId": "3",
                          "subTags": [{"#frameId": "2", "subTags": [{"#type": "PlaceObject2Tag", "~depth": "4"}]}]}]}
        self.assertEqual(process_patch_data(data), [
            {"filter": "/displayRect", "changes": {"Xmax": "1"}},
            {"filter": "/tags/item[@type='DefineSpriteTag'][@spriteId='3']/subTags/frame[@frameId='2']"
                       "/subTags/item[@type='PlaceObject2Tag']", "changes": {"depth": "4"}},
        ])

    def test_split_unsplit_frames_roundtrip(self):
        xml = ("<swf><tags><item type='DefineSpriteTag' spriteId='3'><subTags>"
               "<item type='A'/><item type='ShowFrameTag'/><item type='B'/><item type='ShowFrameTag'/>"
               "</subTags></item></tags></swf>")
        root = split_frames(ET.fromstring(xml))
        frames = root.findall(".//frame")
        self.assertEqual([f.get("frameId") for f in frames], ["1", "2"])
        self.assertEqual(frames[1].find("subTags/item").get("type"), "B")
        back = unsplit_frames(root)
        self.assertEqual([i.get("type") for i in back.find(".//subTags")], ["A", "ShowFrameTag", "B", "ShowFrameTag"])

    def test_resolve_patch_root_named_patch(self):
        with tempfile.TemporaryDirectory() as d:
            inner = Path(d) / "patch" / "Patch" / "interface"
            inner.mkdir(parents=True)
            (inner / "x.json").write_text("{}")
            self.assertEqual(resolve_patch_root(Path(d) / "patch"), Path(d) / "patch")
            self.assertEqual(resolve_patch_root(Path(d) / "patch" / "Patch"), Path(d) / "patch")
            self.assertEqual([t.describe() for t in patch_targets(Path(d) / "patch")], ["interface/x.swf"])

    def test_split_bsa(self):
        self.assertEqual(_split_bsa(PurePosixPath("RaceMenu.BSA/interface/x.bin")),
                         ("RaceMenu.BSA", PurePosixPath("interface/x.bin")))


def fake_ffdec_zip(path: Path, version: str) -> Path:
    """An FFDec-release-shaped zip whose ffdec.jar carries project.properties."""
    import io
    import zipfile
    jar = io.BytesIO()
    with zipfile.ZipFile(jar, "w") as j:
        j.writestr("project.properties", f"#generated\nversion={version}\n")
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("ffdec.jar", jar.getvalue())
        z.writestr("lib/README", "x")
    return path


class FFDecSelectionTests(unittest.TestCase):
    def setUp(self):
        from dip_core import tools
        self.tools = tools
        self.tmp = Path(tempfile.mkdtemp())
        self._saved = (tools.FFDEC_DIR, tools._SYSTEM_FFDEC, tools._OTHER_CACHES)
        tools.FFDEC_DIR = self.tmp / "ffdec"
        tools._SYSTEM_FFDEC, tools._OTHER_CACHES = (), ()
        for var in ("DIP_FFDEC_JAR", "FFDEC_JAR"):
            os.environ.pop(var, None)

    def tearDown(self):
        self.tools.FFDEC_DIR, self.tools._SYSTEM_FFDEC, self.tools._OTHER_CACHES = self._saved
        shutil.rmtree(self.tmp)

    def test_install_zip_and_pick_wanted_version(self):
        t = self.tools
        jar26 = t.install_ffdec_zip(fake_ffdec_zip(self.tmp / "ffdec_26.4.0.zip", "26.4.0"), log=lambda m: None)
        self.assertEqual(jar26, t.FFDEC_DIR / "26.4.0" / "ffdec.jar")
        self.assertEqual(t.jar_version(jar26), "26.4.0")
        # an 18.5.0 "Windows DIP" jar offered as a hint
        old = self.tmp / "dip" / "ffdec"
        old.mkdir(parents=True)
        with zipfile_open(old / "ffdec.jar") as j:
            j.writestr("project.properties", "version=18.5.0\n")
        self.assertEqual(t.find_ffdec_jar("", [old / "ffdec.jar"], "26.4.0"), jar26)
        self.assertEqual(t.find_ffdec_jar("", [old / "ffdec.jar"], "18.5.0"), old / "ffdec.jar")
        self.assertIsNone(t.find_ffdec_jar("", [old / "ffdec.jar"], "99.0.0"))
        # no download allowed: fall back to what exists, newest install first
        self.assertEqual(t.resolve_ffdec(lambda m: None, "", [old / "ffdec.jar"], "99.0.0", False), jar26)
        # an explicit zip setting installs (once) and uses that version
        jar27 = t.resolve_ffdec(lambda m: None, str(fake_ffdec_zip(self.tmp / "z27.zip", "27.0.0")))
        self.assertEqual(t.jar_version(jar27), "27.0.0")

    def test_rejects_non_ffdec_zip(self):
        import zipfile
        bad = self.tmp / "bad.zip"
        with zipfile.ZipFile(bad, "w") as z:
            z.writestr("readme.txt", "hi")
        with self.assertRaises(self.tools.ToolError):
            self.tools.install_ffdec_zip(bad, log=lambda m: None)


def zipfile_open(path: Path):
    import zipfile
    return zipfile.ZipFile(path, "w")


class XmlPatchTests(unittest.TestCase):
    def test_unmatched_filter_is_skipped_not_written(self):
        with tempfile.TemporaryDirectory() as d:
            xml = Path(d) / "a.xml"
            xml.write_text("<swf><tags><item type='A' id='1'/></tags></swf>")
            logs: list[str] = []
            p = Patcher(PatchOptions(output_folder=Path(d)), log=logs.append)
            p._patch_xml(xml, {"tags": [{"#type": "Missing", "~x": "1"},
                                        {"#type": "A", "#id": "1", "matrix": {"~hasScale": "true"}}]})
            root = ET.parse(xml).getroot()  # still well-formed
            self.assertEqual([c.tag for c in root.find("tags")], ["item"])
            self.assertEqual(root.find("tags/item/matrix").get("hasScale"), "true")  # DIP's create case kept
            self.assertTrue(any("matched nothing" in m for m in logs))


NEW_XML = r"""<swf _xmlExportMajor="4" _xmlExportMinor="2" type="SWF"><tags>
<item type="CSMSettingsTag" textID="11" thickness="0.0" sharpness="0.0" gridFit="2"/>
<item type="PlaceObject2Tag" depth="1" placeFlagHasRatio="false">
  <matrix type="MATRIX" hasScale="false" hasRotate="false" translateX="10" translateY="0"/></item>
<item type="PlaceObject2Tag" depth="2">
  <matrix type="MATRIX" hasScale="true" hasRotate="false" scaleX="1.0113678" scaleY="0.33332825" translateX="0" translateY="0"/></item>
<item type="ImportAssets2Tag" url="obscured\\marker.swf"/>
<item type="DefineFont3Tag" fontId="3" fontName="Futura\u0000"/>
<item type="DefineFontAlignZonesTag"><zoneTable><item type="ZONERECORD"><zoneData>
  <item type="ZONEDATA" alignmentCoordinate="0.0" range="0.33325195"/></zoneData></item></zoneTable></item>
<item type="DefineEditTextTag" characterID="5" fontHeight="400.0" initialText="x"/>
</tags></swf>"""

# written the way DIP patches are: against FFDec 18.5.0's representation
LEGACY_PATCH = {"tags": [
    {"#type": "CSMTextSettingsTag", "#textID": "11", "~thickness": "1.5"},
    {"#type": "PlaceObject2Tag", "#depth": "1", "#ratio": "0",
     "matrix": {"#scaleX": "0", "~translateX": "20"}},
    {"#type": "PlaceObject2Tag", "#depth": "2",
     "matrix": {"#scaleX": "66281", "~scaleX": "131072", "~rotateSkew0": "-65536", "~hasRotate": "true"}},
    {"#type": "ImportAssets2Tag", "#url": "obscured\\marker.swf", "~url": "new\\path.swf"},
    {"#type": "DefineFont3Tag", "#fontName": "Futura", "~fontName": "Line1\nLine2"},
    {"#type": "DefineFontAlignZonesTag", "zoneTable": [{"zoneData": [{"#range": "13653", "~range": "15360"}]}]},
    {"#type": "DefineEditTextTag", "#characterID": "5", "#fontHeight": "400", "~initialText": "ok"},
]}


class FFDecCompatTests(unittest.TestCase):
    def run_patch(self, xml_text: str, patch: dict) -> tuple[ET.Element, list[str]]:
        with tempfile.TemporaryDirectory() as d:
            xml = Path(d) / "a.xml"
            xml.write_text(xml_text)
            logs: list[str] = []
            Patcher(PatchOptions(output_folder=Path(d)), log=logs.append)._patch_xml(xml, patch)
            return ET.parse(xml).getroot(), logs

    def test_legacy_patch_on_new_xml(self):
        root, logs = self.run_patch(NEW_XML, LEGACY_PATCH)
        tags = root.find("tags")
        self.assertEqual(tags[0].get("thickness"), "1.5")                     # CSM alias
        self.assertEqual(tags[1].find("matrix").get("translateX"), "20")       # omitted ratio/scaleX == 0
        m2 = tags[2].find("matrix")
        self.assertEqual((m2.get("scaleX"), m2.get("rotateSkew0")), ("2.0", "-1.0"))  # 16.16 -> decimal
        self.assertEqual(tags[3].get("url"), "new\\\\path.swf")                 # escaped for FFDec 19+
        self.assertEqual(tags[4].get("fontName"), "Line1\\nLine2")
        self.assertEqual(root.find(".//zoneData/item").get("range"), "1.0")   # half-float bits -> decimal
        self.assertEqual(tags[6].get("initialText"), "ok")                     # 400 == 400.0
        self.assertFalse(any("matched nothing" in m for m in logs), logs)
        self.assertTrue(any("translated" in m for m in logs))

    def test_created_element_values_are_converted(self):
        patch = {"tags": [{"#type": "PlaceObject2Tag", "#depth": "1",
                           "colorTransform": {"~type": "CXFORMWITHALPHA", "~alphaMultTerm": "128"},
                           "clipMatrix": {"~type": "MATRIX", "~hasScale": "true", "~scaleX": "131072"}}]}
        root, _ = self.run_patch(NEW_XML, patch)
        m = root.find("tags/item[@depth='1']/clipMatrix")
        self.assertEqual((m.get("type"), m.get("scaleX")), ("MATRIX", "2.0"))
        self.assertEqual(root.find("tags/item[@depth='1']/colorTransform").get("alphaMultTerm"), "128")

    def test_old_xml_keeps_dip_behaviour(self):
        old = NEW_XML.replace(' _xmlExportMajor="4" _xmlExportMinor="2"', "")
        root, logs = self.run_patch(old, LEGACY_PATCH)
        tags = root.find("tags")
        self.assertEqual(tags[0].get("thickness"), "0.0")       # no alias for 18.5.0 XML
        self.assertEqual(tags[3].get("url"), "obscured\\\\marker.swf")  # filter used the literal value
        self.assertTrue(any("matched nothing" in m for m in logs))

    def test_decimals_become_integers_for_18_5(self):
        old = NEW_XML.replace(' _xmlExportMajor="4" _xmlExportMinor="2"', "")
        patch = {"tags": [{"#type": "PlaceObject2Tag", "#depth": "2", "matrix": {"~scaleX": "2.0", "~scaleY": "131072"}},
                          {"#type": "DefineFontAlignZonesTag", "zoneTable": [{"zoneData": [{"~range": "1.0"}]}]}]}
        root, _ = self.run_patch(old, patch)
        m = root.find("tags/item[@depth='2']/matrix")
        self.assertEqual((m.get("scaleX"), m.get("scaleY")), ("131072", "131072"))  # integers kept as-is
        self.assertEqual(root.find(".//zoneData/item").get("range"), "15360")

    def test_impossible_values_rejected_for_both_formats(self):
        old = NEW_XML.replace(' _xmlExportMajor="4" _xmlExportMinor="2"', "")
        for xml in (NEW_XML, old):
            for bad in ("9999999.5", str(1 << 31)):
                with self.assertRaises(ValueError):
                    self.run_patch(xml, {"tags": [{"#type": "PlaceObject2Tag", "#depth": "2",
                                                   "matrix": {"~scaleX": bad}}]})
        root, _ = self.run_patch(NEW_XML, {"tags": [{"#type": "PlaceObject2Tag", "#depth": "2",
                                                     "matrix": {"~scaleX": "16383.5"}}]})
        self.assertEqual(root.find("tags/item[@depth='2']/matrix").get("scaleX"), "16383.5")  # in range: kept

    def test_escape_roundtrip(self):
        from dip_core.ffdec_compat import escape, unescape
        s = "a\\b\nc\td\x00"
        self.assertEqual(escape(s), "a\\\\b\\nc\\td\\u0000")
        self.assertEqual(unescape(escape(s)), s)


class FakeFFDec:
    """Stand-in for tools.FFDec: the 'SWF' is XML text; behaviour is scripted per version."""

    def __init__(self, version: str, *, fail: bool = False, drop: str = "", cancel: bool = False):
        self.version, self.fail, self.drop, self.cancel = version, fail, drop, cancel
        self.passes = 0

    def swf2xml(self, swf: Path, xml: Path) -> None:
        self.passes += 1
        if self.cancel:
            from dip_core import tools
            raise tools.Cancelled("Cancelled.")
        text = swf.read_text()
        xml.write_text(text.replace(self.drop, "") if self.drop else text)

    def xml2swf(self, xml: Path, swf: Path) -> None:
        if self.fail:
            from dip_core import tools
            raise tools.ToolError("FFDec exited with code 1: simulated import failure")
        swf.write_text(xml.read_text().replace("<swf>", f"<swf by='{self.version}'>", 1))

    def replace(self, swf: Path, cmd: Path) -> None:
        pass


ORIGINAL = "<swf><tags><item type='A'><special><inner x='0'/></special></item><item type='B' y='0'/></tags></swf>"
PATCH = {"swf": {"tags": [{"#type": "A", "special": {"inner": {"~x": "1"}}}, {"#type": "B", "~y": "1"}]}}


class FFDecFallbackTests(unittest.TestCase):
    def run_one(self, primary, fallback, enabled=True, patch=PATCH):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d)
        swf = d / "menu.swf"
        swf.write_text(ORIGINAL)
        logs: list[str] = []
        p = Patcher(PatchOptions(output_folder=d, ffdec_fallback=enabled), log=logs.append)
        p.ffdec = primary
        p._fallback_ffdec = lambda: fallback if enabled else None
        used = p._patch_json(swf, patch, d)
        root = ET.fromstring(swf.read_text())
        self.assertEqual(sorted(x.name for x in d.iterdir()), ["menu.swf"])  # no temp copies left
        return used, root, logs

    def test_success_does_not_retry(self):
        fb = FakeFFDec("18.5.0")
        used, root, _ = self.run_one(FakeFFDec("26.4.0"), fb)
        self.assertEqual((used, root.get("by"), fb.passes), ("26.4.0", "26.4.0", 0))

    def test_error_falls_back_from_original(self):
        used, root, logs = self.run_one(FakeFFDec("26.4.0", fail=True), FakeFFDec("18.5.0"))
        self.assertEqual((used, root.get("by")), ("18.5.0", "18.5.0"))
        self.assertEqual(root.find(".//inner").get("x"), "1")
        self.assertEqual(root.find(".//item[@type='B']").get("y"), "1")  # edits applied once, on the original
        self.assertTrue(any("retrying from the original" in m for m in logs))

    def test_unmatched_filters_fall_back(self):
        used, root, _ = self.run_one(FakeFFDec("26.4.0", drop="<special><inner x='0'/></special>"),
                                     FakeFFDec("18.5.0"))
        self.assertEqual((used, root.get("by"), root.find(".//inner").get("x")), ("18.5.0", "18.5.0", "1"))

    def test_keeps_primary_when_fallback_is_no_better(self):
        drop = "<item type='B' y='0'/>"
        used, root, logs = self.run_one(FakeFFDec("26.4.0", drop=drop), FakeFFDec("18.5.0", drop=drop))
        self.assertEqual((used, root.get("by")), ("26.4.0", "26.4.0"))
        self.assertTrue(any("did no better" in m for m in logs))

    def test_keeps_primary_when_fallback_errors(self):
        used, root, _ = self.run_one(FakeFFDec("26.4.0", drop="<item type='B' y='0'/>"),
                                     FakeFFDec("18.5.0", fail=True))
        self.assertEqual((used, root.get("by")), ("26.4.0", "26.4.0"))

    def test_both_fail_raises(self):
        with self.assertRaises(DipError) as cm:
            self.run_one(FakeFFDec("26.4.0", fail=True), FakeFFDec("18.5.0", fail=True))
        self.assertIn("failed too", str(cm.exception))

    def test_disabled_fallback_raises_primary_error(self):
        with self.assertRaises(DipError):
            self.run_one(FakeFFDec("26.4.0", fail=True), FakeFFDec("18.5.0"), enabled=False)

    def test_cancel_is_not_retried(self):
        from dip_core import tools
        fb = FakeFFDec("18.5.0")
        with self.assertRaises(tools.Cancelled):
            self.run_one(FakeFFDec("26.4.0", cancel=True), fb)
        self.assertEqual(fb.passes, 0)


@unittest.skipUnless(XDELTA3, "xdelta3 not installed")
class EndToEndBinaryPatch(unittest.TestCase):
    def test_bin_patch_from_bsa(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            orig_swf = os.urandom(20_000)
            new_swf = orig_swf[:5000] + b"DIP" + orig_swf[5000:]
            (d / "src.swf").write_bytes(orig_swf)
            (d / "new.swf").write_bytes(new_swf)
            mod = d / "Original Mod"
            mod.mkdir()
            bsa.write_archive({"interface/racesex_menu.swf": d / "src.swf"}, mod / "RaceMenu.bsa")
            patch_dir = d / "Some DIP Patch" / "My DIP" / "Patch" / "RaceMenu.bsa" / "Interface"
            patch_dir.mkdir(parents=True)
            subprocess.run([XDELTA3, "-e", "-S", "lzma", "-s", str(d / "src.swf"), str(d / "new.swf"),
                            str(patch_dir / "RaceSex_Menu.bin")], check=True)
            root = resolve_patch_root(d / "Some DIP Patch")
            self.assertEqual([t.describe() for t in patch_targets(root)], ["RaceMenu.bsa/Interface/RaceSex_Menu.swf"])
            out = d / "out"
            res = Patcher(PatchOptions(output_folder=out, xdelta3="builtin"), log=lambda m: None).patch(root, mod)
            self.assertEqual(len(res.written), 1)
            self.assertEqual(res.written[0].read_bytes(), new_swf)


if __name__ == "__main__":
    unittest.main()
