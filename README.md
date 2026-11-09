## Dynamic Interface Patcher for Linux (Amethyst plugin + CLI)

A native Linux port of [Dynamic Interface Patcher (DIP) 2.1.5](https://www.nexusmods.com/skyrimspecialedition/mods/96891)
by Cutleast. It applies DIP patches, such as the Norden, Oathvein or Vel'dun *RaceMenu DIP* patches, to
UI mods like RaceMenu. **You don't need Wine or Proton.**

The Windows DIP bundles a Java runtime, `7z.exe`, `xdelta.exe` and a C++ `glob.dll`. This port replaces them with:

| Windows DIP | Linux port |
| --- | --- |
| `xdelta.exe` | built-in VCDIFF/xdelta3 decoder (LZMA secondary compression, Adler-32 check); falls back to the host `xdelta3` for anything it can't handle |
| bundled `jre.7z` | the host Java (only needed for JSON patches) |
| `ffdec` (18.5.0) + `.bat` wrapper | FFDec **26.4.0** by default, run directly with Java (18.5.0 still selectable) |
| BSA reader using `lz4` | built-in BSA reader/writer (v104/v105) with a pure-Python LZ4 fallback |
| `glob.dll`, Windows paths | case-insensitive path resolution, so Windows-authored patches (`IMG\Foo.png`, `Interface` vs `interface`) work on Linux |

The patch pipeline (load → copy/extract → shapes → SWF→XML → XML edits → XML→SWF → xdelta → output)
is a faithful port of DIP's. Binary patches produce byte-identical output to DIP.exe.

## Install the Amethyst plugin

Copy this folder to `~/.config/AmethystModManager/Plugins/dynamic_interface_patcher/`
(or `$XDG_CONFIG_HOME/AmethystModManager/Plugins/`), then reopen the **Plugins** menu or restart Amethyst.
You need Amethyst 2.6.0 or newer.

## Use it in Amethyst

1. Install the patch mod (e.g. *Vel'dun Racemenu DIP*) and the original mod (e.g. *RaceMenu*) as usual.
2. Open **Plugins → Dynamic Interface Patcher**. It scans the profile and lists every DIP patch, picking as the
   original mod the highest-priority enabled mod that contains the files the patch needs (for example
   `RaceMenu.bsa/interface/racesex_menu.swf`). You can change the original mod or the output mod name per row.
3. Check the patches you want and click **Patch checked**. For each one the plugin:
   * patches the SWFs in a temporary folder,
   * creates a mod named `<patch mod> (DIP Output)` just above the patch or original mod, whichever has the
     higher priority (or reuses that mod if it already exists),
   * installs the patched SWFs into it through Amethyst's own archive installer (so Amethyst's file index sees
     them and deploy includes them) and enables the mod.
4. **Deploy.** Re-run the patch after updating the original mod or the patch.

### Automatic re-patching before launch

Each time you start the game from Amethyst (after its deploy, before the game runs), the plugin checks every
enabled `… (DIP Output)` mod. This replaces the Windows-only **Automatic Patcher** SKSE plugin.

* It fingerprints what the output depends on: the patch's files and the original SWFs in the highest-priority
  enabled mod that provides them (or the original you chose in the panel, while it stays enabled).
* If nothing changed and the output files are untouched, it does nothing. On a real profile the check takes
  about 0.01 s.
* If the patch or the original mod changed (an update, a reinstall under a new name) or the output files were
  modified, it patches again and rewrites the changed files in place. Symlinked, hardlinked and copied
  deployments all show the new content in the same launch, with no redeploy.
* Outputs made before this feature, or by hand, are verified once on the next launch and then tracked.
* It never blocks the game. Errors keep the existing output and go to Amethyst's log (lines start with
  "Dynamic Interface Patcher:"). Pressing Stop during the check still cancels the launch.
* It also logs enabled DIP patches that have no output mod yet, and warns when Automatic Patcher is enabled:
  that plugin runs the Windows DIP.exe through Proton at game start and writes into the game folder behind
  Amethyst.

It does not create new output mods; use the panel for that. When an update changes which files a patch
produces, it asks you to patch again in the panel. Turn it off with the setting **Before launching the game,
re-patch DIP outputs…**.

The **Manual** section works like DIP.exe: it patches any patch folder and original mod folder into any
output folder. Existing output files are renamed with a timestamp, as DIP does.

### Settings (Plugins → Browse plugins → Settings…)

* **Java executable**: leave empty to auto-detect (`$JAVA_HOME`, `PATH`, `/usr/lib/jvm`).
* **FFDec version** (default `26.4.0`): the version to find or download. FFDec installs live in
  `~/.local/share/dynamic-interface-patcher/ffdec/<version>/`. The FFDec inside a Windows DIP mod (18.5.0) is
  used only when you ask for `18.5.0`.
* **FFDec jar or zip**: leave empty for automatic selection. Point it to an `ffdec.jar` to use that jar as-is,
  or to an `ffdec_<version>.zip` release to install and use it.

* **Java heap** and the default for **Repack BSA** (experimental: writes a patched copy of the original BSA into
  the output mod instead of loose files).

#### Using DIP patches with FFDec 26.4.0

Windows DIP (2.1.5 and the 2.2.0 betas) bundles FFDec 18.5.0, and DIP's JSON patches are written against its
XML export. Newer FFDec writes that XML differently, so the patcher translates 18.5.0-style patches
automatically (`dip_core/ffdec_compat.py`):

| Patches expect (FFDec 18.5.0) | FFDec 26.x writes | Translation |
| --- | --- | --- |
| `CSMTextSettingsTag` | `CSMSettingsTag` (same attributes) | tag alias |
| every attribute, e.g. `scaleX="0"` when `hasScale="false"` | unused or default attributes omitted | a filter on `0`, `0.0` or `false` also matches an omitted attribute |
| MATRIX `scaleX`, `scaleY`, `rotateSkew0`, `rotateSkew1` as 16.16 integers (`66281`) | decimals (`1.0113678`) | converted both ways (÷/× 65536) |
| ZONEDATA `alignmentCoordinate`, `range` as half-float bits | decimals | half-float conversion |
| raw strings | backslash-escaped (`\\`, `\n`, `\t`, `\u0000`) | escaped on write, unescaped to compare |

The translation is active only for XML carrying FFDec's `_xmlExportMajor` version marker. With 18.5.0 the
patcher behaves exactly like DIP. In tests on real Vel'dun UI files (`charactersheet.swf` and `gfxfontlib.swf`)
with patches generated from FFDec 18.5.0's export, 26.4.0 produced the same changes as 18.5.0, with an
identical `charactersheet.swf`. 26.4.0 also rebuilds SWFs more faithfully than 18.5.0: it kept all 15 fonts
in `gfxfontlib.swf` intact, while 18.5.0 drops the trailing NUL from 5 font names. It is about twice as fast.

Not translated: structural changes such as metadata or line-style elements that 26.x nests differently. Very
few patches touch those, and the fallback below handles them.

#### Automatic fallback to FFDec 18.5.0

When FFDec 26.4.0 fails on a SWF (an FFDec error, or any patch filter that matches nothing), the patcher
starts again from that SWF's original with FFDec 18.5.0. It then keeps the better result:

| 26.4.0 result | 18.5.0 result | Kept |
| --- | --- | --- |
| error | succeeds | 18.5.0, with a warning to check the file in game (18.5.0 is more lenient) |
| some filters unmatched | fewer unmatched | 18.5.0 |
| some filters unmatched | no better, or an error | 26.4.0 |
| error | error | nothing; the patch fails with both messages |

Each SWF is handled separately, so one fallback doesn't affect the other files in a patch. The log names every
file that used 18.5.0. FFDec 18.5.0 comes from a Windows DIP mod in your mod list when there is one, otherwise
from `~/.local/share/dynamic-interface-patcher/ffdec/18.5.0`, or it is downloaded. Turn the fallback off with
the **If FFDec 26.4.0 fails on a file…** setting or `--no-ffdec-fallback`.

Two safeguards keep the more lenient 18.5.0 from hiding errors:

* Matrix scale/rotation and font-zone values outside what a SWF can store (a scale of 16384 or more, a
  half-float above 65504) are rejected before either FFDec runs.
* 18.5.0 reads those fields only as integers and silently writes 0 for anything else, so decimal values are
  converted to its integer form first. This is the one place the patcher deliberately differs from DIP.exe,
  which would write 0.

Java and FFDec are only needed for JSON patches. Binary (`.bin`) patches, which include all three RaceMenu DIP
patches above, need nothing beyond Python.

## Command line

The command line takes the same arguments as DIP.exe and runs on any Python 3.10+, with no extra packages:

```bash
python3 dip_cli.py "<patch mod or patch folder>" "<original mod folder>" -o "<output folder>"
python3 dip_cli.py --list "/path/to/Amethyst Games/Skyrim Special Edition/mods"   # find patches
```

```bash
python3 dip_cli.py --install-ffdec ~/Downloads/ffdec_26.4.0.zip                      # install an FFDec release
```

Options: `-b/--repack-bsa`, `-d/--debug`, `--no-backup`, `--java`, `--ffdec JAR|ZIP`, `--ffdec-version`,
`--no-ffdec-fallback`, `--install-ffdec ZIP`, `--no-download`, `--xdelta3`.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

## License

GPL-3.0, like the original DIP (see `LICENSE`). FFDec (JPEXS) is GPL-3.0/LGPL-3.0 and is not bundled here.
