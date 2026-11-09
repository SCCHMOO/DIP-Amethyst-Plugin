"""
External tools: Java + FFDec (JPEXS Free Flash Decompiler) and, optionally,
the host's ``xdelta3``.

The Windows DIP shipped a private JRE (jre.7z) and a .bat wrapper around
ffdec.jar.  Here we use the host's Java directly.

FFDec selection (:func:`resolve_ffdec`):

1. an explicit jar (plugin setting / ``--ffdec``) is used whatever its version;
   an explicit ``.zip`` is installed first,
2. ``$DIP_FFDEC_JAR`` / ``$FFDEC_JAR``,
3. otherwise the first FFDec of the *wanted version* (default 26.4.0) among:
   our installs in ``~/.local/share/dynamic-interface-patcher/ffdec/<version>``,
   FFDecs the caller found (e.g. inside a Windows DIP mod, which ship 18.5.0),
   and system packages,
4. otherwise the wanted version is downloaded from GitHub,
5. if downloading is disabled or fails, any FFDec found above (with a warning).

DIP 2.1.5 bundles FFDec 18.5.0.  26.4.0 produces the same results for typical
patches and round-trips SWFs more faithfully, but its XML export omits some
default-valued attributes; set the version to 18.5.0 if a patch's filters stop
matching.
"""

from __future__ import annotations

import functools
import os
import shutil
import subprocess
import threading
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional

LogFn = Callable[[str], None]

FFDEC_VERSION = "26.4.0"          # default
DIP_FFDEC_VERSION = "18.5.0"      # what Windows DIP 2.1.5 / 2.2.0 bundle
FFDEC_URL = ("https://github.com/jindrapetrik/jpexs-decompiler/releases/download/"
             "version{v}/ffdec_{v}.zip")
_DATA_HOME = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local/share"))
DATA_DIR = _DATA_HOME / "dynamic-interface-patcher"
FFDEC_DIR = DATA_DIR / "ffdec"

JAVA_HELP = ("Java was not found. Install a Java runtime (Arch/CachyOS: sudo pacman -S jre-openjdk, "
             "Debian/Ubuntu: sudo apt install default-jre, Fedora: sudo dnf install java-latest-openjdk) "
             "or set the Java executable in the settings.")


class ToolError(RuntimeError):
    pass


class Cancelled(ToolError):
    pass


# --------------------------------------------------------------------------- #
# Environment: never leak AppImage-injected variables into host programs.
# --------------------------------------------------------------------------- #
_BUNDLE_PREFIXES = (
    "APPDIR", "APPIMAGE", "OWD", "URUNTIME", "ARG0", "ARGV0", "SHARUN_",
    "LD_LIBRARY_PATH", "LD_PRELOAD", "GCONV_PATH",
    "PYTHONHOME", "PYTHONPATH", "PYTHONDONTWRITEBYTECODE",
    "FONTCONFIG_", "SSL_CERT_FILE", "SSL_CERT_DIR", "CURL_CA_BUNDLE",
    "GDK_PIXBUF_MODULEDIR", "GDK_PIXBUF_MODULE_FILE", "GIO_MODULE_DIR",
    "GSETTINGS_SCHEMA_DIR", "GTK_PATH", "QT_PLUGIN_PATH", "QML2_IMPORT_PATH",
    "QT_QPA_PLATFORM_PLUGIN_PATH", "TERMINFO", "JAVA_TOOL_OPTIONS",
)


def host_env() -> dict[str, str]:
    env = dict(os.environ)
    try:  # Amethyst's own scrubber, when running inside the manager
        from Utils.environment.appimage import strip_appimage_vars  # type: ignore
        env = dict(strip_appimage_vars(env))
    except Exception:  # noqa: BLE001
        pass
    appdir = os.path.realpath(env["APPDIR"]) if env.get("APPDIR") else ""
    out = {k: v for k, v in env.items() if not k.startswith(_BUNDLE_PREFIXES)}
    for var in ("PATH", "XDG_DATA_DIRS", "XDG_CONFIG_DIRS"):
        if var in out:
            parts = [p for p in out[var].split(":")
                     if p and "/.mount_" not in p and not (appdir and p.startswith(appdir))]
            out[var] = ":".join(parts)
    out.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    return out


def _which(name: str) -> Optional[str]:
    return shutil.which(name, path=host_env().get("PATH"))


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #
def find_java(custom: str = "") -> Optional[str]:
    if custom:
        p = Path(custom).expanduser()
        return str(p) if p.is_file() and os.access(p, os.X_OK) else None
    home = os.environ.get("JAVA_HOME")
    if home and os.access(Path(home) / "bin" / "java", os.X_OK):
        return str(Path(home) / "bin" / "java")
    found = _which("java")
    if found:
        return found
    for cand in sorted(Path("/usr/lib/jvm").glob("*/bin/java")):
        if os.access(cand, os.X_OK):
            return str(cand)
    return None


def find_xdelta3(custom: str = "") -> Optional[str]:
    if custom:
        p = Path(custom).expanduser()
        return str(p) if p.is_file() and os.access(p, os.X_OK) else None
    return _which("xdelta3")


_SYSTEM_FFDEC = ("/usr/share/java/ffdec/ffdec.jar", "/usr/share/ffdec/ffdec.jar",
                 "/opt/ffdec/ffdec.jar", "/usr/share/jpexs-decompiler/ffdec.jar")
_OTHER_CACHES = (_DATA_HOME / "completionist-gui-patcher" / "ffdec",)  # sibling Amethyst plugin


@functools.lru_cache(maxsize=64)
def jar_version(jar: Path) -> Optional[str]:
    """FFDec version from ``project.properties`` in ffdec.jar or lib/ffdec_lib.jar."""
    for cand in (Path(jar), Path(jar).parent / "lib" / "ffdec_lib.jar"):
        try:
            with zipfile.ZipFile(cand) as z:
                for line in z.read("project.properties").decode("utf-8", "replace").splitlines():
                    if line.startswith("version="):
                        return line.split("=", 1)[1].strip()
        except (OSError, KeyError, zipfile.BadZipFile):
            continue
    return None


def _explicit_jar(custom: str) -> Optional[Path]:
    p = Path(custom).expanduser()
    if p.is_dir():
        p = p / "ffdec.jar"
    return p if p.is_file() else None


def ffdec_candidates(hints: Iterable[Path] = ()) -> list[Path]:
    """Every FFDec jar we know about, in preference order (no version filtering)."""
    out: list[Path] = []
    if FFDEC_DIR.is_dir():
        for d in sorted(FFDEC_DIR.iterdir(), reverse=True):
            if (d / "ffdec.jar").is_file():
                out.append(d / "ffdec.jar")
        if (FFDEC_DIR / "ffdec.jar").is_file():  # layout used by earlier builds
            out.append(FFDEC_DIR / "ffdec.jar")
    out += [Path(h) for h in hints if h and Path(h).is_file()]
    out += [d / "ffdec.jar" for d in _OTHER_CACHES if (d / "ffdec.jar").is_file()]
    out += [Path(c) for c in _SYSTEM_FFDEC if Path(c).is_file()]
    seen, uniq = set(), []
    for p in out:
        if p.resolve() not in seen:
            seen.add(p.resolve())
            uniq.append(p)
    return uniq


def find_ffdec_jar(custom: str = "", hints: Iterable[Path] = (),
                   version: str = FFDEC_VERSION) -> Optional[Path]:
    """The FFDec to use without downloading: explicit/env jar, else one of ``version``."""
    if custom and not custom.lower().endswith(".zip"):
        return _explicit_jar(custom)
    for var in ("DIP_FFDEC_JAR", "FFDEC_JAR"):
        v = os.environ.get(var)
        if v and Path(v).is_file():
            return Path(v)
    for jar in ffdec_candidates(hints):
        if not version or jar_version(jar) == version:
            return jar
    return None


def resolve_ffdec(log: LogFn, custom: str = "", hints: Iterable[Path] = (),
                  version: str = FFDEC_VERSION, allow_download: bool = True) -> Path:
    """Find (installing or downloading if needed) the FFDec jar to run."""
    version = (version or FFDEC_VERSION).strip()
    if custom and custom.lower().endswith(".zip"):
        zp = Path(custom).expanduser()
        zv = _zip_version(zp)
        installed = FFDEC_DIR / zv / "ffdec.jar" if zv else None
        return installed if installed and installed.is_file() else install_ffdec_zip(zp, log)
    if custom:
        jar = _explicit_jar(custom)
        if jar is None:
            raise ToolError(f"FFDec jar not found: {custom}")
        return jar
    jar = find_ffdec_jar("", hints, version)
    if jar is not None:
        return jar
    others = ffdec_candidates(hints)
    if allow_download:
        try:
            return download_ffdec(log, version)
        except ToolError as e:
            if not others:
                raise
            log(f"Warning: {e}")
    if not others:
        raise ToolError(f"FFDec {version} was not found and downloading is disabled.")
    log(f"Warning: FFDec {version} is not installed; using FFDec {jar_version(others[0]) or '?'} "
        f"from {others[0]}")
    return others[0]


def find_exact_ffdec(log: LogFn, hints: Iterable[Path], version: str,
                     allow_download: bool = True) -> Optional[Path]:
    """FFDec of exactly ``version`` (installed, hinted or downloaded), else None - never another version."""
    jar = next((j for j in ffdec_candidates(hints) if jar_version(j) == version), None)
    if jar is not None:
        return jar
    return download_ffdec(log, version) if allow_download else None


def _zip_version(zip_path: Path) -> Optional[str]:
    try:
        with zipfile.ZipFile(zip_path) as z:
            names = set(z.namelist())
            for inner in ("ffdec.jar", "lib/ffdec_lib.jar"):
                if inner in names:
                    import io
                    with zipfile.ZipFile(io.BytesIO(z.read(inner))) as j:
                        for line in j.read("project.properties").decode("utf-8", "replace").splitlines():
                            if line.startswith("version="):
                                return line.split("=", 1)[1].strip()
    except (OSError, KeyError, zipfile.BadZipFile):
        pass
    return None


def install_ffdec_zip(zip_path: Path, log: LogFn = print) -> Path:
    """Install an FFDec release zip (``ffdec_<version>.zip``) into ``FFDEC_DIR/<version>``."""
    zip_path = Path(zip_path).expanduser()
    if not zip_path.is_file():
        raise ToolError(f"FFDec zip not found: {zip_path}")
    version = _zip_version(zip_path)
    if not version:
        raise ToolError(f"{zip_path.name} is not an FFDec release zip (no ffdec.jar with a version).")
    dest = FFDEC_DIR / version
    tmp = FFDEC_DIR / f".{version}.partial"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    try:
        root = tmp.resolve()
        with zipfile.ZipFile(zip_path) as z:
            for m in z.namelist():  # zip-slip guard
                if not (tmp / m).resolve().is_relative_to(root):
                    raise ToolError(f"Unsafe path in FFDec archive: {m}")
            z.extractall(tmp)
        for script in ("ffdec.sh", "ffdec"):
            if (tmp / script).is_file():
                (tmp / script).chmod(0o755)
        shutil.rmtree(dest, ignore_errors=True)
        tmp.rename(dest)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    log(f"FFDec {version} installed to {dest}")
    return dest / "ffdec.jar"


def windows_dip_ffdec(folder: Path) -> Optional[Path]:
    """Return ``ffdec.jar`` of a Windows DIP install inside ``folder`` (a mod folder)."""
    for cand in (folder / "DIP" / "ffdec" / "ffdec.jar", folder / "ffdec" / "ffdec.jar",
                 folder / "DIP" / "res" / "ffdec" / "ffdec.jar", folder / "res" / "ffdec" / "ffdec.jar"):
        if cand.is_file() and (cand.parent / "lib").is_dir():
            return cand
    return None


def download_ffdec(log: LogFn, version: str = FFDEC_VERSION) -> Path:
    url = FFDEC_URL.format(v=version)
    log(f"Downloading FFDec {version} from {url}")
    FFDEC_DIR.mkdir(parents=True, exist_ok=True)
    zpath = FFDEC_DIR / f"ffdec_{version}.zip.part"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "dynamic-interface-patcher-linux"})
        with urllib.request.urlopen(req, timeout=120) as r, open(zpath, "wb") as f:
            shutil.copyfileobj(r, f)
        return install_ffdec_zip(zpath, log)
    except ToolError:
        raise
    except Exception as e:  # noqa: BLE001
        raise ToolError(f"Could not download FFDec {version} ({e}). Download ffdec_{version}.zip from "
                        "https://github.com/jindrapetrik/jpexs-decompiler/releases and set it as the FFDec "
                        "setting (a .zip is installed automatically).") from e
    finally:
        zpath.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# Running FFDec
# --------------------------------------------------------------------------- #
@dataclass
class FFDec:
    java: str
    jar: Path
    memory: str = "1024m"
    log: LogFn = print
    cancel: Optional[threading.Event] = None

    @property
    def version(self) -> str:
        return jar_version(Path(self.jar)) or "unknown"

    def run(self, args: list[str], timeout: float = 900) -> None:
        cmd = [self.java, f"-Xmx{self.memory}", "-Djava.awt.headless=true", "-Djna.nosys=true",
               "-jar", str(self.jar), *args]
        env = host_env()
        env["VLC_VERBOSE"] = "-1"
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, errors="replace", env=env)
        except OSError as e:
            raise ToolError(f"Could not start Java: {e}") from e
        waited = 0.0
        while True:
            try:
                out, err = proc.communicate(timeout=0.25)
                break
            except subprocess.TimeoutExpired:
                waited += 0.25
                if (self.cancel is not None and self.cancel.is_set()) or waited > timeout:
                    proc.kill()
                    proc.communicate()
                    if waited > timeout:
                        raise ToolError("FFDec timed out.") from None
                    raise Cancelled("Cancelled.") from None
        text = "\n".join(s for s in (out.strip(), err.strip()) if s)
        if proc.returncode != 0:
            lines = [l.strip() for l in text.splitlines() if l.strip()]
            # prefer Java's own cause ("...Exception: message") over stack-frame lines
            causes = [l for l in lines if ("Exception" in l or "Error" in l) and not l.startswith("at ")]
            detail = causes[-1] if causes else " | ".join(lines[-3:])
            raise ToolError(f"FFDec exited with code {proc.returncode}: {detail}")
        # FFDec sometimes reports failures on stderr with exit code 0
        for line in err.splitlines():
            if "SEVERE" in line or "Exception" in line:
                self.log(f"FFDec: {line.strip()}")

    def swf2xml(self, swf: Path, xml: Path) -> None:
        self.run(["-swf2xml", str(swf), str(xml)])
        if not xml.is_file():
            raise ToolError(f"FFDec did not write {xml.name}")

    def xml2swf(self, xml: Path, swf: Path) -> None:
        self.run(["-xml2swf", str(xml), str(swf)])
        if not swf.is_file():
            raise ToolError(f"FFDec did not write {swf.name}")

    def replace(self, swf: Path, commands_file: Path) -> None:
        self.run(["-replace", str(swf), str(swf), str(commands_file)])


def run_xdelta3(binary: str, source: Path, patch: Path, output: Path) -> None:
    proc = subprocess.run([binary, "-d", "-f", "-s", str(source), str(patch), str(output)],
                          stdin=subprocess.DEVNULL, capture_output=True, text=True,
                          errors="replace", env=host_env(), timeout=600)
    if proc.returncode != 0:
        raise ToolError(f"xdelta3 failed: {(proc.stderr or proc.stdout).strip()}")
