#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Opus AudioBook Compressor
=========================

Batch-compress audiobooks (MP3, M4B, M4A, FLAC, WAV, ...) to speech-optimised
Opus using FFmpeg + libopus (the reference Opus encoder).

Put this script next to your audiobook folders and run it:

    python opus_audiobook_compressor.py

Requirements: Python 3.8+, FFmpeg and FFprobe (built with libopus).
No third-party Python packages are needed.

Licensed under the MIT License.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import logging
import os
import platform
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.request
import zipfile
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Deque, Dict, List, Optional, Set, Tuple

APP_NAME = "Opus AudioBook Compressor"
APP_VERSION = "1.1.0"

# =============================================================================
# USER CONFIGURATION  (safe to edit)
# =============================================================================

DEFAULT_THREADS = 8              # parallel FFmpeg processes (can be changed at runtime)
DEFAULT_PRESET_INDEX = 2         # 1-based index into PRESETS below
INCLUDE_SUBDIRS_DEFAULT = True   # process sub-directories by default
SOUND_DEFAULT = True             # beep when finished (1 beep) or on errors (3 beeps)
OUTPUT_SUFFIX = "_opus"          # suffix of the output folder in "save as new" mode
LOGS_DIR_NAME = "logs"           # log files are written to <script folder>/<this>

# Output duration must match the source within max(seconds, fraction * duration),
# otherwise the file is reported as failed and the original is never deleted.
DURATION_TOLERANCE = (5.0, 0.03)

# Files with these extensions are converted. Everything else is only copied
# (cover.jpg, .cue, .pdf, already-converted .opus, ...) in "save as new" mode.
AUDIO_EXTENSIONS = {
    ".mp3", ".m4a", ".m4b", ".aac", ".flac", ".wav", ".wave", ".ogg", ".oga",
    ".wma", ".ape", ".wv", ".aif", ".aiff", ".mka", ".alac", ".ac3", ".amr",
    ".mp2", ".mpc", ".tta", ".caf", ".webm",
}

# Folders that are never offered for processing.
IGNORED_FOLDERS = {"__pycache__", "venv", "env", "node_modules", "ffmpeg", LOGS_DIR_NAME}


# -----------------------------------------------------------------------------
# PRESETS  -  add, remove or edit entries freely. Every field maps directly to
# an FFmpeg/libopus option. Only `name`, `bitrate_kbps` and `description`
# are required, the rest has sensible defaults.
#
#   name            Shown in the menu.
#   bitrate_kbps    Target bitrate (VBR average). 64 kbps mono is transparent or
#                   near-transparent for speech in Opus.
#   description     One-line hint shown in the menu.
#   application     libopus `-application`: "voip" (speech heuristics, same as the
#                   "Voice" mode of most converters), "audio" (music/general) or
#                   "lowdelay".
#   channels        1 = mono (recommended for speech), 2 = stereo.
#   frame_duration  `-frame_duration` in ms: 2.5, 5, 10, 20, 40, 60 (80/100/120 with
#                   FFmpeg >= 5). Values > 20 only help the SILK/hybrid modes, i.e.
#                   low bitrates; they are harmless for audiobooks (no live use).
#   vbr             "on" (VBR, best quality/size), "constrained" or "off" (CBR).
#   compression_level  0-10, encoder complexity. 10 = best quality, still fast.
#   extra_args      Extra raw FFmpeg arguments, e.g. ("-af", "highpass=f=60").
# -----------------------------------------------------------------------------
@dataclass(frozen=True)
class Preset:
    name: str
    bitrate_kbps: int
    description: str
    application: str = "voip"
    channels: int = 1
    frame_duration: float = 20
    vbr: str = "on"
    compression_level: int = 10
    extra_args: Tuple[str, ...] = ()

    def summary(self) -> str:
        ch = "mono" if self.channels == 1 else "stereo" if self.channels == 2 else f"{self.channels}ch"
        vbr = {"on": "VBR", "constrained": "CVBR", "off": "CBR"}.get(self.vbr, self.vbr)
        app = "voice" if self.application == "voip" else self.application
        return f"{self.bitrate_kbps} kbps, {ch}, {vbr}, {app}"


PRESETS: List[Preset] = [
    Preset("Maximum",   96, "Near-lossless voice; best for dramatised books with music/effects",
           application="audio"),
    Preset("High",      64, "Excellent voice quality (recommended default)"),
    Preset("Balanced",  48, "Very good voice quality, ~25% smaller than High"),
    Preset("Compact",   32, "Good voice quality, half the size of High",
           frame_duration=40),
    Preset("Tiny",      24, "Clean speech only; slight artefacts on noisy recordings",
           frame_duration=60),
]

# =============================================================================
# END OF USER CONFIGURATION
# =============================================================================

MODE_COPY = "copy"
MODE_REPLACE = "replace"
COVER_CODECS = {"mjpeg", "png"}
CREATE_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
FFMPEG_WIN_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"

log = logging.getLogger("opus_compressor")
log.setLevel(logging.DEBUG)
log.addHandler(logging.NullHandler())


# =============================================================================
# Terminal UI helpers
# =============================================================================
STYLES = {
    "bold": "1", "gray": "90", "red": "91", "green": "92", "yellow": "93",
    "blue": "94", "magenta": "95", "cyan": "96", "white": "97",
}


class UI:
    color = False
    unicode = True
    lock = threading.RLock()
    bar_active = False
    last_len = 0


def paint(text: str, *styles: str) -> str:
    codes = [STYLES[s] for s in styles if s in STYLES]
    if not UI.color or not codes:
        return text
    return "\033[" + ";".join(codes) + "m" + text + "\033[0m"


def enable_ansi() -> bool:
    """Enable ANSI/VT escape sequences (needed on Windows 10 consoles)."""
    if os.environ.get("NO_COLOR") or not sys.stdout.isatty():
        return False
    if os.name == "nt":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            handle = kernel32.GetStdHandle(-11)
            mode = ctypes.c_uint32()
            if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                return False
            if not kernel32.SetConsoleMode(handle, mode.value | 0x0004):
                return False
        except Exception:
            return False
    return True


def term_width() -> int:
    return max(40, shutil.get_terminal_size((100, 24)).columns)


def _clear_bar_line() -> None:
    if UI.bar_active:
        if UI.color:
            sys.stdout.write("\r\033[K")
        else:
            sys.stdout.write("\r" + " " * UI.last_len + "\r")
        UI.bar_active = False


def out(text: str = "") -> None:
    with UI.lock:
        _clear_bar_line()
        sys.stdout.write(text + "\n")
        sys.stdout.flush()


def ui_info(text: str) -> None:
    out("  " + text)


def ui_ok(text: str) -> None:
    out("  " + paint("[ OK ] ", "green", "bold") + text)


def ui_warn(text: str) -> None:
    log.warning(text)
    out("  " + paint("[WARN] ", "yellow", "bold") + paint(text, "yellow"))


def ui_error(text: str) -> None:
    log.error(text)
    out("  " + paint("[FAIL] ", "red", "bold") + paint(text, "red"))


def rule(char: Optional[str] = None) -> None:
    char = char or ("─" if UI.unicode else "-")
    out(paint(char * min(term_width() - 1, 78), "gray"))


def heading(text: str) -> None:
    out()
    out(paint("  " + text, "magenta", "bold"))
    rule()


def disp_width(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else
               0 if unicodedata.combining(c) else 1 for c in s)


def fit(s: str, width: int) -> str:
    """Truncate with an ellipsis to a given display width."""
    if disp_width(s) <= width:
        return s
    ell = "…" if UI.unicode else "..."
    res, w = "", 0
    for c in s:
        cw = disp_width(c)
        if w + cw > width - len(ell):
            break
        res, w = res + c, w + cw
    return res + ell


def pad(s: str, width: int, right: bool = False) -> str:
    gap = max(0, width - disp_width(s))
    return (" " * gap + s) if right else (s + " " * gap)


def fmt_size(n: float) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} TB"


def fmt_time(sec: float) -> str:
    sec = int(max(0, sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def natural_key(s: str):
    return [int(t) if t.isdigit() else t.casefold() for t in re.split(r"(\d+)", s)]


def ask(prompt: str, default: str = "") -> str:
    """Prompt the user. Ctrl+C / EOF exits the program cleanly."""
    with UI.lock:
        _clear_bar_line()
        sys.stdout.write("  " + prompt + " ")
        sys.stdout.flush()
    try:
        ans = input().strip()
    except (EOFError, KeyboardInterrupt):
        out()
        raise SystemExit(0)
    return ans if ans else default


def ask_yes_no(prompt: str, default: bool) -> bool:
    hint = paint("[Y/n]" if default else "[y/N]", "cyan")
    while True:
        ans = ask(f"{prompt} {hint}:", "").lower()
        if not ans:
            return default
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no"):
            return False
        ui_warn("Please answer 'y' or 'n'.")


# =============================================================================
# FFmpeg discovery / installation
# =============================================================================
def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def find_tool(name: str, extra_dir: Optional[Path] = None) -> Optional[str]:
    exe = name + (".exe" if os.name == "nt" else "")
    base = app_dir()
    candidates = []
    if extra_dir:
        candidates.append(extra_dir / exe)
    candidates += [base / exe, base / "ffmpeg" / exe, base / "ffmpeg" / "bin" / exe]
    for c in candidates:
        if c.is_file():
            return str(c)
    return shutil.which(name)


def run_quiet(cmd: List[str], timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, creationflags=CREATE_FLAGS)


def check_ffmpeg(ffmpeg: str) -> Tuple[bool, str]:
    try:
        ver = run_quiet([ffmpeg, "-version"]).stdout.splitlines()[0]
        enc = run_quiet([ffmpeg, "-hide_banner", "-encoders"]).stdout
        return ("libopus" in enc), ver
    except Exception as e:  # noqa: BLE001
        return False, f"error: {e}"


def download_ffmpeg_windows(dest: Path) -> bool:
    dest.mkdir(parents=True, exist_ok=True)
    zip_path = dest / "ffmpeg_download.zip"
    try:
        req = urllib.request.Request(FFMPEG_WIN_URL, headers={"User-Agent": f"{APP_NAME}/{APP_VERSION}"})
        with urllib.request.urlopen(req, timeout=60) as resp, open(zip_path, "wb") as fh:
            total = int(resp.headers.get("Content-Length") or 0)
            got, last = 0, 0.0
            while True:
                chunk = resp.read(1 << 16)
                if not chunk:
                    break
                fh.write(chunk)
                got += len(chunk)
                if time.time() - last > 0.2:
                    last = time.time()
                    pct = f"{got * 100 / total:5.1f}%" if total else ""
                    sys.stdout.write(f"\r  Downloading FFmpeg... {fmt_size(got)} {pct}   ")
                    sys.stdout.flush()
        sys.stdout.write("\n")
        with zipfile.ZipFile(zip_path) as z:
            for name in z.namelist():
                low = name.lower()
                if low.endswith("/bin/ffmpeg.exe") or low.endswith("/bin/ffprobe.exe"):
                    with z.open(name) as src, open(dest / Path(name).name, "wb") as dst:
                        shutil.copyfileobj(src, dst)
        return (dest / "ffmpeg.exe").is_file() and (dest / "ffprobe.exe").is_file()
    except Exception as e:  # noqa: BLE001
        ui_error(f"Automatic FFmpeg download failed: {e}")
        return False
    finally:
        try:
            zip_path.unlink()
        except OSError:
            pass


def ensure_ffmpeg(custom: Optional[str]) -> Tuple[str, str, str]:
    extra: Optional[Path] = None
    if custom:
        p = Path(custom)
        extra = p if p.is_dir() else p.parent
    while True:
        ffmpeg = find_tool("ffmpeg", extra)
        ffprobe = find_tool("ffprobe", extra)
        if ffmpeg and ffprobe:
            has_opus, version = check_ffmpeg(ffmpeg)
            if has_opus:
                return ffmpeg, ffprobe, version
            ui_error(f"The FFmpeg found at '{ffmpeg}' has no libopus encoder. Use a full build.")
        else:
            ui_error("FFmpeg / FFprobe not found (searched next to the script and in PATH).")

        out()
        if os.name == "nt":
            ui_info("Install options for Windows:")
            ui_info("  " + paint("winget install Gyan.FFmpeg", "cyan") + "   (then restart this script)")
            ui_info("  or let this script download a build (~100 MB) into the 'ffmpeg' folder next to it.")
            if ask_yes_no("Download FFmpeg automatically now?", True):
                if download_ffmpeg_windows(app_dir() / "ffmpeg"):
                    ui_ok("FFmpeg installed.")
                    continue
        elif sys.platform == "darwin":
            ui_info("Install with Homebrew: " + paint("brew install ffmpeg", "cyan"))
        else:
            ui_info("Install with your package manager, e.g.:")
            ui_info("  " + paint("sudo apt install ffmpeg", "cyan") + "   |   " +
                    paint("sudo dnf install ffmpeg", "cyan") + "   |   " +
                    paint("sudo pacman -S ffmpeg", "cyan"))
        pause_before_exit()
        raise SystemExit(1)


def pause_before_exit() -> None:
    """Keep the window open on Windows when started by double-click."""
    if os.name != "nt":
        return
    try:
        import ctypes
        procs = (ctypes.c_uint32 * 4)()
        if ctypes.windll.kernel32.GetConsoleProcessList(procs, 4) <= 2:  # type: ignore[attr-defined]
            input("  Press Enter to close...")
    except Exception:  # noqa: BLE001
        pass


# =============================================================================
# Data model
# =============================================================================
class ConvError(Exception):
    def __init__(self, message: str, detail: str = ""):
        super().__init__(message)
        self.detail = detail


class Cancelled(Exception):
    pass


@dataclass
class FileEntry:
    path: Path
    size: int
    is_audio: bool


@dataclass
class FolderInfo:
    path: Path
    entries: List[FileEntry]

    def visible(self, recursive: bool) -> List[FileEntry]:
        if recursive:
            return self.entries
        return [e for e in self.entries if e.path.parent == self.path]

    def audio(self, recursive: bool) -> List[FileEntry]:
        return [e for e in self.visible(recursive) if e.is_audio]


@dataclass
class ProbeInfo:
    duration: float
    duration_known: bool
    audio_index: int
    covers: List[int]
    bitrate: int = 0
    cover_info: Optional[Tuple[str, int, int]] = None   # (codec, width, height) of the first cover


@dataclass
class Job:
    src: Path
    dst: Path
    root: Path
    rel_parent: Tuple[str, ...]
    size: int
    duration: float = 1.0
    probe: Optional[ProbeInfo] = None
    status: str = "pending"        # pending / ok / resumed / failed / cancelled
    out_size: int = 0
    message: str = ""
    warnings: List[str] = field(default_factory=list)

    @property
    def rel_name(self) -> str:
        return "/".join(self.rel_parent + (self.src.name,))


@dataclass
class Settings:
    preset_index: int = DEFAULT_PRESET_INDEX
    recursive: bool = INCLUDE_SUBDIRS_DEFAULT
    mode: str = MODE_COPY
    threads: int = DEFAULT_THREADS
    sound: bool = SOUND_DEFAULT


# =============================================================================
# Scanning folders
# =============================================================================
def _ignored(name: str) -> bool:
    return name.startswith(".") or name in IGNORED_FOLDERS


def scan_folder(root: Path) -> List[FileEntry]:
    entries: List[FileEntry] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted([d for d in dirnames if not _ignored(d)], key=natural_key)
        for fn in sorted(filenames, key=natural_key):
            if fn.startswith(".") or fn.endswith(".part"):
                continue
            p = Path(dirpath) / fn
            try:
                size = p.stat().st_size
            except OSError:
                continue
            entries.append(FileEntry(p, size, p.suffix.lower() in AUDIO_EXTENSIONS))
    return entries


def list_folders(base: Path) -> List[FolderInfo]:
    result = []
    try:
        children = sorted((c for c in base.iterdir() if c.is_dir()), key=lambda c: natural_key(c.name))
    except OSError as e:
        ui_error(f"Cannot read the script folder: {e}")
        return []
    for child in children:
        if _ignored(child.name) or child.name.casefold().endswith(OUTPUT_SUFFIX.casefold()):
            continue
        entries = scan_folder(child)
        if any(e.is_audio for e in entries):
            result.append(FolderInfo(child, entries))
    return result


# =============================================================================
# Sound notification (no external files: system beep only)
# =============================================================================
def beep(kind: str) -> None:
    """kind='done' -> 1 beep, kind='error' -> 3 beeps. Never raises."""
    count, freq, length, gap = (1, 1000, 450, 0.0) if kind == "done" else (3, 750, 220, 0.12)
    try:
        if os.name == "nt":
            import winsound
            for i in range(count):
                try:
                    winsound.Beep(freq, length)
                except RuntimeError:
                    winsound.MessageBeep()
                    time.sleep(0.25)
                if i < count - 1:
                    time.sleep(gap)
        elif sys.stdout.isatty():
            for i in range(count):
                with UI.lock:
                    sys.stdout.write("\a")
                    sys.stdout.flush()
                if i < count - 1:
                    time.sleep(0.3)
    except Exception:  # noqa: BLE001
        pass


def notify(st: "Settings", kind: str) -> None:
    if st.sound:
        beep(kind)


# =============================================================================
# Wizard steps
# =============================================================================
def banner() -> None:
    out()
    out(paint("  " + APP_NAME, "cyan", "bold") + paint(f"  v{APP_VERSION}", "gray"))
    out(paint("  Voice-optimised Opus compression for audiobooks", "gray"))
    rule("═" if UI.unicode else "=")


def choose_preset(st: Settings) -> Preset:
    heading("1. Choose a compression preset")
    nw = max(disp_width(p.name) for p in PRESETS)
    for i, p in enumerate(PRESETS, 1):
        mark = paint(" (default)", "gray") if i == st.preset_index else ""
        out("  " + paint(f"{i}", "cyan", "bold") + "  " + paint(pad(p.name, nw), "white", "bold") + "  " +
            paint(pad(p.summary(), 34), "green") + paint(p.description, "gray") + mark)
    while True:
        ans = ask(f"Preset number {paint(f'[{st.preset_index}]', 'cyan')}:", str(st.preset_index))
        if ans.isdigit() and 1 <= int(ans) <= len(PRESETS):
            st.preset_index = int(ans)
            return PRESETS[st.preset_index - 1]
        ui_warn(f"Enter a number from 1 to {len(PRESETS)}.")


def parse_selection(text: str, count: int) -> Optional[List[int]]:
    """'1,3-5' -> [1,3,4,5]; returns None when invalid."""
    picked: Set[int] = set()
    for part in re.split(r"[,\s;]+", text.strip()):
        if not part:
            continue
        m = re.fullmatch(r"(\d+)(?:\s*-\s*(\d+))?", part)
        if not m:
            return None
        a = int(m.group(1))
        b = int(m.group(2)) if m.group(2) else a
        if a > b:
            a, b = b, a
        if a < 1 or b > count:
            return None
        picked.update(range(a, b + 1))
    return sorted(picked) if picked else None


def choose_folders(base: Path, st: Settings) -> Optional[List[FolderInfo]]:
    heading("2. Choose folders")
    while True:
        out(paint("  Scanning...", "gray"))
        all_folders = list_folders(base)
        sys.stdout.write("\033[1A\033[K" if UI.color else "")
        folders = [f for f in all_folders if f.audio(st.recursive)]
        hidden = len(all_folders) - len(folders)

        out("  " + paint("Location: ", "gray") + str(base))
        out("  " + paint("Subfolders: ", "gray") +
            (paint("INCLUDED", "green", "bold") if st.recursive else paint("NOT included", "yellow", "bold")))
        out("  " + paint("Sound:      ", "gray") +
            (paint("ON", "green", "bold") if st.sound else paint("OFF", "yellow", "bold")))
        out()
        if not folders:
            ui_warn("No folders with audio files were found next to the script.")
        else:
            nw = min(48, max(disp_width(f.path.name) for f in folders))
            out(paint("  " + pad("#", 4) + pad("Folder", nw + 2) + pad("Files", 7, True) + pad("Size", 12, True), "gray"))
            for i, f in enumerate(folders, 1):
                au = f.audio(st.recursive)
                out("  " + paint(pad(str(i), 4), "cyan", "bold") + paint(pad(fit(f.path.name, nw), nw + 2), "white") +
                    pad(str(len(au)), 7, True) + paint(pad(fmt_size(sum(e.size for e in au)), 12, True), "green"))
        if hidden:
            out(paint(f"  ({hidden} folder(s) hidden: audio only in sub-folders - press S to include them)", "gray"))
        out()
        out("  " + "  ".join([
            paint("Enter", "cyan", "bold") + "=all",
            paint("1,3-5", "cyan", "bold") + "=pick",
            paint("R", "cyan", "bold") + "=refresh",
            paint("S", "cyan", "bold") + "=toggle subfolders",
            paint("B", "cyan", "bold") + "=toggle sound",
            paint("Q", "cyan", "bold") + "=quit",
        ]))
        ans = ask("Your choice " + paint("[all]", "cyan") + ":", "").lower()
        if ans in ("", "a", "all"):
            if folders:
                return folders
            ui_warn("Nothing to process.")
        elif ans in ("r", "refresh"):
            out(paint("  Refreshing folder list...", "gray"))
        elif ans in ("s", "subdirs"):
            st.recursive = not st.recursive
        elif ans in ("b", "sound", "beep"):
            st.sound = not st.sound
            if st.sound:
                beep("done")
        elif ans in ("q", "quit", "exit"):
            return None
        else:
            idx = parse_selection(ans, len(folders)) if folders else None
            if idx:
                return [folders[i - 1] for i in idx]
            ui_warn("Invalid selection. Examples: 2   or   1,3   or   2-4   or   1,3-5")


def choose_mode(st: Settings) -> str:
    heading("3. Output mode")
    out("  " + paint("1", "cyan", "bold") + f'  Save into new folders with the "{OUTPUT_SUFFIX}" suffix  ' +
        paint("(originals untouched, default)", "green"))
    out("  " + paint("2", "cyan", "bold") + "  Replace originals  " +
        paint("(each original is deleted after its Opus file is verified)", "yellow"))
    default = "1" if st.mode == MODE_COPY else "2"
    while True:
        ans = ask(f"Mode {paint(f'[{default}]', 'cyan')}:", default)
        if ans in ("1", "2"):
            st.mode = MODE_COPY if ans == "1" else MODE_REPLACE
            return st.mode
        ui_warn("Enter 1 or 2.")


def choose_threads(st: Settings) -> int:
    heading("4. Parallel threads")
    cpus = os.cpu_count() or 1
    out(paint(f"  This machine reports {cpus} logical CPU(s).", "gray"))
    while True:
        ans = ask(f"Number of threads {paint(f'[{st.threads}]', 'cyan')}:", str(st.threads))
        if ans.isdigit() and 1 <= int(ans) <= 64:
            st.threads = int(ans)
            return st.threads
        ui_warn("Enter a number from 1 to 64.")


# =============================================================================
# Job planning
# =============================================================================
def build_jobs(folders: List[FolderInfo], st: Settings, base: Path) -> Tuple[List[Job], List[Tuple[Path, Path]]]:
    jobs: List[Job] = []
    extras: List[Tuple[Path, Path]] = []
    for folder in folders:
        out_root = base / (folder.path.name + OUTPUT_SUFFIX)
        used: Set[str] = set()
        for e in folder.visible(st.recursive):
            rel = e.path.relative_to(folder.path)
            target_dir = (out_root / rel.parent) if st.mode == MODE_COPY else e.path.parent
            if not e.is_audio:
                if st.mode == MODE_COPY:
                    extras.append((e.path, out_root / rel))
                continue
            dst = target_dir / (e.path.stem + ".opus")
            if str(dst).casefold() in used or (st.mode == MODE_REPLACE and dst.exists()):
                dst = target_dir / (e.path.name + ".opus")   # avoid name collisions
            used.add(str(dst).casefold())
            job = Job(src=e.path, dst=dst, root=folder.path, rel_parent=tuple(rel.parent.parts), size=e.size)
            if st.mode == MODE_COPY and dst.is_file() and dst.stat().st_size > 0:
                job.status, job.out_size = "resumed", dst.stat().st_size
            jobs.append(job)
    return jobs, extras


def probe_file(ffprobe: str, path: Path) -> ProbeInfo:
    try:
        r = subprocess.run([ffprobe, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=180, creationflags=CREATE_FLAGS)
    except subprocess.TimeoutExpired:
        raise ConvError("Analysing the file took too long (timeout).")
    if r.returncode != 0:
        raise ConvError(explain_error(r.stderr, r.returncode, probing=True), r.stderr)
    try:
        data = json.loads(r.stdout or "{}")
    except ValueError:
        raise ConvError("FFprobe returned unreadable data for this file.", r.stdout[:500])
    streams = data.get("streams", [])
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    if not audio:
        raise ConvError("No audio stream found in the file.")
    cover_streams = [s for s in streams
                     if s.get("codec_type") == "video"
                     and s.get("disposition", {}).get("attached_pic") == 1
                     and s.get("codec_name") in COVER_CODECS]
    covers = [s["index"] for s in cover_streams]
    cover_info = None
    if cover_streams:
        c = cover_streams[0]
        cover_info = (c.get("codec_name", ""), int(c.get("width") or 0), int(c.get("height") or 0))

    def num(v) -> float:
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    dur = num(data.get("format", {}).get("duration")) or num(audio[0].get("duration"))
    return ProbeInfo(duration=dur, duration_known=dur > 0, audio_index=audio[0]["index"],
                     covers=covers, bitrate=int(num(audio[0].get("bit_rate"))), cover_info=cover_info)


def probe_all(jobs: List[Job], ffprobe: str, threads: int) -> None:
    todo = [j for j in jobs if j.status == "pending"]
    if not todo:
        return
    counter = {"n": 0}
    lock = threading.Lock()

    def work(job: Job) -> None:
        try:
            job.probe = probe_file(ffprobe, job.src)
            job.duration = job.probe.duration if job.probe.duration_known else max(1.0, job.size / 16000.0)
        except ConvError as e:
            job.status, job.message = "failed", str(e)
            log.error("PROBE FAILED %s: %s | %s", job.src, e, e.detail.strip()[:500])
        except Exception as e:  # noqa: BLE001
            job.status, job.message = "failed", f"Unexpected error while analysing: {e}"
            log.exception("PROBE CRASH %s", job.src)
        with lock:
            counter["n"] += 1
            n = counter["n"]
        with UI.lock:
            sys.stdout.write("\r  " + paint("Analysing files... ", "cyan") + f"{n}/{len(todo)}   ")
            sys.stdout.flush()

    with ThreadPoolExecutor(max_workers=max(1, min(threads, 16))) as ex:
        list(ex.map(work, todo))
    sys.stdout.write("\r" + " " * 60 + "\r")
    for j in todo:
        if j.status == "failed":
            ui_error(f"{j.rel_name}: {j.message}")


# =============================================================================
# Conversion
# =============================================================================
ERROR_PATTERNS = [
    ("moov atom not found", "Incomplete MP4/M4B file ('moov' atom missing) - the file is probably truncated."),
    ("Invalid data found when processing input", "The file is corrupted or is not a valid audio file."),
    ("being used by another process", "The file is locked by another program. Close it and try again."),
    ("Permission denied", "Permission denied. Check that the file/folder is not read-only or locked."),
    ("No space left on device", "Not enough free disk space."),
    ("There is not enough space on the disk", "Not enough free disk space."),
    ("Unknown encoder", "This FFmpeg build has no libopus encoder."),
    ("Encoder libopus not found", "This FFmpeg build has no libopus encoder."),
    ("matches no streams", "No audio stream found in the file."),
    ("does not contain any stream", "No audio stream found in the file."),
    ("No such file or directory", "File not found (was it moved or deleted while processing?)."),
    ("Input/output error", "Disk read/write error (the drive may be failing or disconnected)."),
    ("Operation not permitted", "Operation not permitted by the operating system."),
    ("Filename too long", "The path is too long for the file system."),
    ("File name too long", "The path is too long for the file system."),
    ("Invalid argument", "FFmpeg rejected an argument. Check the preset's settings."),
    ("Unrecognized option", "FFmpeg rejected a preset option. Check 'extra_args' in the preset."),
    ("Option not found", "FFmpeg rejected a preset option. Check 'extra_args' in the preset."),
]


def explain_error(stderr: str, rc: int, probing: bool = False) -> str:
    text = stderr or ""
    for needle, msg in ERROR_PATTERNS:
        if probing and needle in ("Invalid argument", "Unrecognized option", "Option not found"):
            continue
        if needle.lower() in text.lower():
            return msg
    if probing:
        return "The file is corrupted or is not a valid audio file (FFprobe could not read it)."
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    last = f" Last FFmpeg message: {lines[-1][:160]}" if lines else ""
    return f"FFmpeg failed (exit code {rc}).{last}"


class Progress:
    def __init__(self, total_seconds: float, total_files: int):
        self.lock = threading.Lock()
        self.total = max(total_seconds, 1e-6)
        self.total_files = total_files
        self.current: Dict[int, float] = {}
        self.completed = 0.0
        self.ok = 0
        self.failed = 0
        self.t0 = time.time()

    def start(self, job: Job) -> None:
        with self.lock:
            self.current[id(job)] = 0.0

    def update(self, job: Job, secs: float) -> None:
        with self.lock:
            if id(job) in self.current:
                self.current[id(job)] = min(max(secs, 0.0), job.duration)

    def drop(self, job: Job) -> None:
        with self.lock:
            self.current.pop(id(job), None)

    def finish(self, job: Job, ok: bool) -> None:
        with self.lock:
            self.current.pop(id(job), None)
            self.completed += job.duration
            if ok:
                self.ok += 1
            else:
                self.failed += 1

    def snapshot(self) -> dict:
        with self.lock:
            done_sec = self.completed + sum(self.current.values())
            elapsed = max(time.time() - self.t0, 1e-6)
            speed = done_sec / elapsed
            frac = min(1.0, done_sec / self.total)
            eta = (self.total - done_sec) / speed if speed > 0 and done_sec > 0 else 0
            return dict(frac=frac, done=self.ok + self.failed, active=len(self.current),
                        failed=self.failed, speed=speed, eta=eta, elapsed=elapsed)


def draw_progress(p: Progress, final: bool = False) -> None:
    s = p.snapshot()
    width = term_width() - 1
    full, empty = ("█", "░") if UI.unicode else ("#", "-")
    pct_txt = f"{s['frac'] * 100:5.1f}%"
    eta = fmt_time(s["eta"]) if not final else fmt_time(s["elapsed"])
    eta_label = "ETA " if not final else "Time "
    fail_txt = f"  {s['failed']} failed" if s["failed"] else ""
    if width >= 78:
        info = f"{s['done']}/{p.total_files} files  {s['active']} active  {s['speed']:.0f}x  {eta_label}{eta}{fail_txt}"
    else:
        info = f"{s['done']}/{p.total_files}  {eta_label}{eta}{fail_txt}"
    bar_w = max(10, min(40, width - len(info) - len(pct_txt) - 8))
    filled = int(round(bar_w * s["frac"]))
    info_painted = info.replace(fail_txt, paint(fail_txt, "red", "bold")) if fail_txt else info
    line = ("  " + paint(full * filled, "green") + paint(empty * (bar_w - filled), "gray") + " " +
            paint(pct_txt, "yellow", "bold") + "  " + info_painted)
    plain_len = 2 + bar_w + 1 + len(pct_txt) + 2 + len(info)
    with UI.lock:
        sys.stdout.write("\r" + line + ("\033[K" if UI.color else " " * max(0, UI.last_len - plain_len)))
        UI.last_len = plain_len
        UI.bar_active = True
        if final:
            sys.stdout.write("\n")
            UI.bar_active = False
        sys.stdout.flush()


def extract_cover_block(ctx_ffmpeg: str, job: Job) -> Optional[str]:
    """Return the cover as a base64 FLAC METADATA_BLOCK_PICTURE string (the standard way
    to store pictures in Ogg Opus), or None. FFmpeg cannot mux attached pictures into
    Opus itself, so the picture is injected through a temporary FFMETADATA file."""
    p = job.probe
    if not p or not p.covers or not p.cover_info:
        return None
    codec, width, height = p.cover_info
    r = subprocess.run([ctx_ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-i", str(job.src),
                        "-map", f"0:{p.covers[0]}", "-c", "copy", "-frames:v", "1", "-f", "image2pipe", "pipe:1"],
                       capture_output=True, timeout=120, creationflags=CREATE_FLAGS)
    data = r.stdout
    if r.returncode != 0 or not data:
        raise ConvError("Cover art could not be extracted.", r.stderr.decode("utf-8", "replace"))
    mime = b"image/png" if codec == "png" else b"image/jpeg"
    block = (struct.pack(">I", 3) + struct.pack(">I", len(mime)) + mime + struct.pack(">I", 0) +
             struct.pack(">IIII", width, height, 24, 0) + struct.pack(">I", len(data)) + data)
    return base64.b64encode(block).decode("ascii")


def write_cover_metadata_file(b64: str) -> Path:
    value = b64.replace("\\", "\\\\").replace("=", "\\=").replace(";", "\\;").replace("#", "\\#")
    fd, name = tempfile.mkstemp(prefix="opus_cover_", suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(";FFMETADATA1\nMETADATA_BLOCK_PICTURE=" + value + "\n")
    return Path(name)


@dataclass
class Context:
    ffmpeg: str
    ffprobe: str
    preset: Preset
    mode: str
    progress: Progress
    stop: threading.Event = field(default_factory=threading.Event)
    procs: Set[subprocess.Popen] = field(default_factory=set)
    proc_lock: threading.Lock = field(default_factory=threading.Lock)


def build_command(ctx: Context, job: Job, part: Path, cover_meta: Optional[Path]) -> List[str]:
    p, pr = job.probe, ctx.preset
    assert p is not None
    cmd = [ctx.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "warning", "-y",
           "-progress", "pipe:1", "-nostats", "-i", str(job.src)]
    if cover_meta:
        cmd += ["-i", str(cover_meta)]
    cmd += ["-map", f"0:{p.audio_index}", "-map_metadata", "0"]
    if cover_meta:
        cmd += ["-map_metadata:s:a:0", "1:g"]
    cmd += ["-map_chapters", "0",
            # container-specific leftovers that are meaningless in Opus
            "-metadata", "major_brand=", "-metadata", "minor_version=", "-metadata", "compatible_brands="]
    cmd += ["-c:a", "libopus", "-b:a", f"{pr.bitrate_kbps}k", "-vbr", pr.vbr,
            "-application", pr.application, "-compression_level", str(pr.compression_level),
            "-frame_duration", f"{pr.frame_duration:g}", "-ac", str(pr.channels), "-ar", "48000"]
    cmd += list(pr.extra_args)
    cmd += ["-f", "opus", str(part)]
    return cmd


def run_ffmpeg(ctx: Context, cmd: List[str], job: Job) -> Tuple[int, str]:
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace", bufsize=1,
                            creationflags=CREATE_FLAGS)
    with ctx.proc_lock:
        ctx.procs.add(proc)
    tail: Deque[str] = deque(maxlen=40)

    def drain() -> None:
        try:
            for line in proc.stderr:  # type: ignore[union-attr]
                tail.append(line.rstrip())
        except Exception:  # noqa: BLE001
            pass

    t = threading.Thread(target=drain, daemon=True)
    t.start()
    try:
        for line in proc.stdout:  # type: ignore[union-attr]
            if ctx.stop.is_set():
                break
            if line.startswith(("out_time_us=", "out_time_ms=")):
                try:
                    ctx.progress.update(job, int(line.split("=", 1)[1]) / 1_000_000)
                except ValueError:
                    pass
        if ctx.stop.is_set():
            proc.terminate()
        rc = proc.wait()
    finally:
        with ctx.proc_lock:
            ctx.procs.discard(proc)
        t.join(timeout=5)
    return rc, "\n".join(tail)


def safe_unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def process_job(ctx: Context, job: Job) -> None:
    if ctx.stop.is_set():
        return
    prog = ctx.progress
    prog.start(job)
    part = job.dst.with_name(job.dst.name + ".part")
    ok = False
    try:
        assert job.probe is not None
        job.dst.parent.mkdir(parents=True, exist_ok=True)
        cover_meta: Optional[Path] = None
        try:
            b64 = extract_cover_block(ctx.ffmpeg, job)
            if b64:
                cover_meta = write_cover_metadata_file(b64)
        except ConvError as e:
            job.warnings.append(f"{e} Converting without cover art.")
            log.warning("Cover extraction failed: %s | %s", job.src, e.detail[-300:])
        except Exception as e:  # noqa: BLE001
            job.warnings.append(f"Cover art skipped: {e}")
        attempts = [cover_meta, None] if cover_meta else [None]
        success, rc, tail = False, -1, ""
        try:
            for meta in attempts:
                prog.update(job, 0)
                safe_unlink(part)
                rc, tail = run_ffmpeg(ctx, build_command(ctx, job, part, meta), job)
                if ctx.stop.is_set():
                    raise Cancelled()
                if rc == 0 and part.exists() and part.stat().st_size > 0:
                    success = True
                    if cover_meta and meta is None:
                        job.warnings.append("Cover art could not be embedded; audio was converted without it.")
                    break
                if meta:
                    log.warning("Retrying without cover art: %s | %s", job.src, tail[-400:])
        finally:
            if cover_meta:
                safe_unlink(cover_meta)
        if not success:
            raise ConvError(explain_error(tail, rc), tail)

        out_info = probe_file(ctx.ffprobe, part)
        if job.probe.duration_known:
            tol = max(DURATION_TOLERANCE[0], DURATION_TOLERANCE[1] * job.probe.duration)
            if abs(out_info.duration - job.probe.duration) > tol:
                raise ConvError(
                    f"Output length ({fmt_time(out_info.duration)}) differs from the source "
                    f"({fmt_time(job.probe.duration)}); the source may be damaged. Original kept.")
        os.replace(part, job.dst)
        job.out_size = job.dst.stat().st_size
        if ctx.mode == MODE_REPLACE:
            try:
                os.remove(job.src)
            except OSError as e:
                job.warnings.append(f"Converted, but the original could not be deleted: {e}")
        if job.out_size >= job.size:
            job.warnings.append("Output is larger than the source (source bitrate is lower than the preset).")
        job.status, ok = "ok", True
        log.info("OK  %s -> %s  [%s -> %s]  %s", job.src, job.dst, fmt_size(job.size),
                 fmt_size(job.out_size), fmt_time(job.duration))
    except Cancelled:
        job.status = "cancelled"
    except ConvError as e:
        job.status, job.message = "failed", str(e)
        log.error("FAILED %s: %s\n--- FFmpeg output ---\n%s\n---------------------", job.src, e, e.detail)
    except Exception as e:  # noqa: BLE001
        job.status, job.message = "failed", f"Unexpected error: {e}"
        log.exception("CRASH %s", job.src)
    finally:
        safe_unlink(part)
        if job.status == "cancelled":
            prog.drop(job)
        else:
            prog.finish(job, ok)
    if job.status == "failed":
        ui_error(f"{job.rel_name}: {job.message}")
    for w in job.warnings:
        ui_warn(f"{job.rel_name}: {w}")


def run_conversion(jobs: List[Job], ctx: Context, threads: int) -> bool:
    """Returns True if finished, False if cancelled."""
    pending = sorted((j for j in jobs if j.status == "pending"), key=lambda j: j.size, reverse=True)
    if not pending:
        return True
    ex = ThreadPoolExecutor(max_workers=threads)
    futures = [ex.submit(process_job, ctx, j) for j in pending]
    finished = True
    try:
        while ctx.progress.snapshot()["done"] < len(pending):
            draw_progress(ctx.progress)
            time.sleep(0.15)
        draw_progress(ctx.progress, final=True)
    except KeyboardInterrupt:
        finished = False
        ctx.stop.set()
        out()
        ui_warn("Cancelling... stopping running FFmpeg processes.")
        for f in futures:
            f.cancel()
        with ctx.proc_lock:
            for pr in list(ctx.procs):
                try:
                    pr.terminate()
                except OSError:
                    pass
    finally:
        ex.shutdown(wait=True)
    return finished


def copy_extras(extras: List[Tuple[Path, Path]]) -> None:
    if not extras:
        return
    copied = 0
    for src, dst in extras:
        try:
            if dst.exists() and dst.stat().st_size == src.stat().st_size:
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            copied += 1
        except OSError as e:
            ui_warn(f"Could not copy '{src.name}': {e}")
    if copied:
        ui_info(f"Copied {paint(str(copied), 'cyan', 'bold')} non-audio file(s) (covers, cue sheets, ...).")
        log.info("Copied %d extra files", copied)


# =============================================================================
# Report
# =============================================================================
@dataclass
class Node:
    files: int = 0
    before: int = 0
    after: int = 0
    failed: int = 0


def saved_text(before: int, after: int) -> Tuple[str, str]:
    if before <= 0:
        return "-", "gray"
    pct = (1 - after / before) * 100
    if pct >= 0:
        return f"{pct:.1f}% smaller", "green"
    return f"{-pct:.1f}% larger", "red"


def print_report(folders: List[FolderInfo], jobs: List[Job], elapsed: float, cancelled: bool) -> None:
    heading("Results" + (" (cancelled - partial)" if cancelled else ""))
    nodes: Dict[Tuple[Path, Tuple[str, ...]], Node] = {}
    for j in jobs:
        for depth in range(len(j.rel_parent) + 1):
            n = nodes.setdefault((j.root, j.rel_parent[:depth]), Node())
            if j.status in ("ok", "resumed"):
                n.files += 1
                n.before += j.size
                n.after += j.out_size
            elif j.status == "failed":
                n.failed += 1

    rows: List[Tuple[str, Node, bool]] = []
    for f in folders:
        keys = sorted((k for k in nodes if k[0] == f.path), key=lambda k: [natural_key(x) for x in k[1]])
        for _, parts in keys:
            n = nodes[(f.path, parts)]
            if n.files == 0 and n.failed == 0:
                continue
            if not parts:
                rows.append((f.path.name, n, True))
            else:
                prefix = "  " * (len(parts) - 1) + ("└─ " if UI.unicode else "+- ")
                rows.append((prefix + parts[-1], n, False))

    label_w = min(46, max([disp_width("Folder")] + [disp_width(r[0]) for r in rows]))
    show_fail = any(r[1].failed for r in rows)
    header = pad("Folder", label_w) + pad("Files", 7, True) + pad("Before", 12, True) + pad("After", 12, True) + "  Saved"
    out(paint("  " + header, "gray"))
    for label, n, is_root in rows:
        txt, color = saved_text(n.before, n.after)
        line = ("  " + paint(pad(fit(label, label_w), label_w), "white", "bold") if is_root else
                "  " + paint(pad(fit(label, label_w), label_w), "gray"))
        line += pad(str(n.files), 7, True) + pad(fmt_size(n.before), 12, True) + \
            paint(pad(fmt_size(n.after), 12, True), "cyan") + "  " + paint(txt, color, "bold" if is_root else "")
        if show_fail and n.failed:
            line += "  " + paint(f"{n.failed} failed", "red", "bold")
        out(line)

    ok_jobs = [j for j in jobs if j.status in ("ok", "resumed")]
    failed = [j for j in jobs if j.status == "failed"]
    skipped = [j for j in jobs if j.status in ("pending", "cancelled")]
    resumed = [j for j in jobs if j.status == "resumed"]
    before = sum(j.size for j in ok_jobs)
    after = sum(j.out_size for j in ok_jobs)
    txt, color = saved_text(before, after)
    rule()
    out("  " + paint("TOTAL  ", "magenta", "bold") + f"{len(ok_jobs)} file(s):  " + paint(fmt_size(before), "white", "bold") +
        (" → " if UI.unicode else " -> ") + paint(fmt_size(after), "cyan", "bold") + "   " + paint(txt, color, "bold"))
    out(f"  Elapsed: {fmt_time(elapsed)}")
    if resumed:
        ui_info(f"{len(resumed)} file(s) were already converted earlier and were skipped (included in totals).")
    if skipped:
        ui_warn(f"{len(skipped)} file(s) were not processed.")
    if failed:
        out()
        out("  " + paint(f"{len(failed)} file(s) FAILED:", "red", "bold"))
        for j in failed:
            out("   " + paint("- ", "red") + f"{j.rel_name}: " + paint(j.message, "red"))
    else:
        if ok_jobs:
            ui_ok("All files were converted successfully.")
    log.info("TOTAL ok=%d failed=%d not_processed=%d before=%s after=%s (%s) elapsed=%s",
             len(ok_jobs), len(failed), len(skipped), fmt_size(before), fmt_size(after), txt, fmt_time(elapsed))


# =============================================================================
# Logging
# =============================================================================
def setup_logging(base: Path) -> Tuple[logging.Handler, Path]:
    logs = base / LOGS_DIR_NAME
    try:
        logs.mkdir(exist_ok=True)
        path = logs / f"opus_compressor_{dt.datetime.now():%Y%m%d_%H%M%S}.log"
        handler: logging.Handler = logging.FileHandler(path, encoding="utf-8")
    except OSError:
        path = Path(os.devnull)
        handler = logging.NullHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S"))
    log.addHandler(handler)
    return handler, path


# =============================================================================
# Main
# =============================================================================
def run_once(base: Path, st: Settings, ffmpeg: str, ffprobe: str, ffversion: str) -> bool:
    """One full wizard + conversion pass. Returns False when user quit at the folder step."""
    preset = choose_preset(st)
    folders = choose_folders(base, st)
    if folders is None:
        return False
    mode = choose_mode(st)
    threads = choose_threads(st)

    files = [e for f in folders for e in f.audio(st.recursive)]
    heading("Summary")
    rows = [
        ("Preset", f"{preset.name} ({preset.summary()})"),
        ("Folders", f"{len(folders)} selected, {len(files)} audio file(s), {fmt_size(sum(e.size for e in files))}"),
        ("Subfolders", "included" if st.recursive else "not included"),
        ("Output", f'new folders with "{OUTPUT_SUFFIX}" suffix' if mode == MODE_COPY
         else "REPLACE originals (deleted after verified conversion)"),
        ("Threads", str(threads)),
        ("Sound", "on" if st.sound else "off"),
    ]
    for k, v in rows:
        colour = "yellow" if (k == "Output" and mode == MODE_REPLACE) else "white"
        out("  " + paint(pad(k, 11), "gray") + paint(v, colour, "bold" if colour == "yellow" else ""))
    out()
    if mode == MODE_REPLACE:
        out("  " + paint("WARNING: original files will be permanently deleted.", "red", "bold"))
        if ask("Type YES to confirm:", "").strip().upper() != "YES":
            ui_info("Cancelled.")
            return True
    elif not ask_yes_no("Start?", True):
        ui_info("Cancelled.")
        return True

    handler, log_path = setup_logging(base)
    try:
        log.info("%s v%s | %s | Python %s", APP_NAME, APP_VERSION, platform.platform(), platform.python_version())
        log.info("FFmpeg: %s | %s", ffmpeg, ffversion)
        log.info("Preset: %s | mode=%s | threads=%d | subdirs=%s", preset, mode, threads, st.recursive)
        log.info("Folders: %s", ", ".join(str(f.path) for f in folders))

        jobs, extras = build_jobs(folders, st, base)
        out()
        probe_all(jobs, ffprobe, threads)
        todo = [j for j in jobs if j.status == "pending"]
        total_dur = sum(j.duration for j in todo)
        ui_info(f"To convert: {paint(str(len(todo)), 'cyan', 'bold')} file(s), "
                f"{paint(fmt_size(sum(j.size for j in todo)), 'cyan', 'bold')}, "
                f"audio length {paint(fmt_time(total_dur), 'cyan', 'bold')}")
        if mode == MODE_COPY and todo:
            est = total_dur * preset.bitrate_kbps * 125 * 1.05
            try:
                free = shutil.disk_usage(base).free
                if free < est:
                    ui_warn(f"Free disk space ({fmt_size(free)}) may be lower than the estimated "
                            f"output size ({fmt_size(est)}).")
            except OSError:
                pass
        out()

        start = time.time()
        cancelled = False
        if todo:
            ctx = Context(ffmpeg, ffprobe, preset, mode, Progress(total_dur, len(todo)))
            cancelled = not run_conversion(jobs, ctx, threads)
        if mode == MODE_COPY and not cancelled:
            copy_extras(extras)
        print_report(folders, jobs, time.time() - start, cancelled)
        if not cancelled:
            has_problems = any(j.status in ("failed", "pending", "cancelled") for j in jobs)
            notify(st, "error" if has_problems else "done")
        ui_info("Log file: " + paint(str(log_path), "cyan"))
    finally:
        log.removeHandler(handler)
        handler.close()
    return True


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=f"{APP_NAME} - voice-optimised Opus compression for audiobooks")
    ap.add_argument("--threads", type=int, help=f"default number of threads (default {DEFAULT_THREADS})")
    ap.add_argument("--ffmpeg", help="path to the ffmpeg binary or to the folder containing ffmpeg and ffprobe")
    ap.add_argument("--no-color", action="store_true", help="disable colours")
    ap.add_argument("--no-sound", action="store_true", help="disable the beep notifications")
    ap.add_argument("--ascii", action="store_true", help="use plain ASCII characters only")
    return ap.parse_args()


def main() -> None:
    args = parse_args()
    try:
        sys.stdout.reconfigure(errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass
    UI.color = enable_ansi() and not args.no_color
    UI.unicode = not args.ascii
    if os.name == "nt":
        os.system("title " + APP_NAME)

    base = app_dir()
    st = Settings()
    if args.threads and 1 <= args.threads <= 64:
        st.threads = args.threads
    if args.no_sound:
        st.sound = False

    banner()
    ffmpeg, ffprobe, ffversion = ensure_ffmpeg(args.ffmpeg)
    ui_info(paint("FFmpeg: ", "gray") + ffversion[:70])

    try:
        first = True
        while True:
            if not first:
                banner()
            first = False
            if not run_once(base, st, ffmpeg, ffprobe, ffversion):
                break
            out()
            if not ask_yes_no("Start over? (the folder list will be re-read)", False):
                break
    except KeyboardInterrupt:
        out()
    except SystemExit:
        pass
    except Exception as e:  # noqa: BLE001
        ui_error(f"Unexpected error: {e}")
        log.exception("Unhandled exception")
        notify(st, "error")
        pause_before_exit()
        raise SystemExit(1)
    out(paint("  Bye!", "cyan"))


if __name__ == "__main__":
    main()
