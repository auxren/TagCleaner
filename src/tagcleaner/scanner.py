"""Find concert folders beneath a root directory.

A "concert folder" is any directory that directly contains audio files, or
whose single subdirectory does (a common pattern where folders are named once
and the extracted archive nests the same name inside). We treat each such
folder as a candidate and pair it with the largest non-fingerprint .txt file
inside for parser input.
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from .parser import build_concert
from .models import Concert

AUDIO_EXTS = {".flac", ".mp3", ".m4a", ".ogg", ".opus", ".wav"}
FINGERPRINT_EXT_HINTS = ("ffp", "md5", "sha", "shntool", "audiochecker", "sbeok")


def iter_concert_folders(root: Path) -> Iterator[tuple[Path, list[Path], Path | None]]:
    """Yield (folder, audio_files, info_txt) for each concert-like folder."""
    if not root.is_dir():
        return
    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        audio, nested = _collect_audio(entry)
        if not audio:
            continue
        info = _pick_info_txt(entry if not nested else nested)
        yield entry, audio, info


def _collect_audio(folder: Path) -> tuple[list[Path], Path | None]:
    """Return (audio files, nested folder if audio lives one level down)."""
    direct = _audio_in(folder)
    if direct:
        return direct, None
    # Look one level deeper — common "folder/folder/*.flac" pattern.
    for child in sorted(folder.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        inner = _audio_in(child)
        if inner:
            return inner, child
    return [], None


def _audio_in(folder: Path) -> list[Path]:
    try:
        return sorted(
            p for p in folder.iterdir()
            if p.is_file()
            and p.suffix.lower() in AUDIO_EXTS
            and not p.name.startswith("._")
        )
    except OSError:
        return []


def _pick_info_txt(folder: Path) -> Path | None:
    """Return the most likely info.txt in *folder*: largest .txt that doesn't
    look like a fingerprint/checksum manifest, or None."""
    candidates: list[Path] = []
    try:
        for p in folder.iterdir():
            if not p.is_file() or p.name.startswith("._"):
                continue
            if p.suffix.lower() != ".txt":
                continue
            low = p.name.lower()
            if any(h in low for h in FINGERPRINT_EXT_HINTS):
                continue
            candidates.append(p)
    except OSError:
        return None
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.stat().st_size, reverse=True)
    return candidates[0]


def scan(root: Path) -> list[Concert]:
    return [build_concert(folder, audio, info) for folder, audio, info in iter_concert_folders(root)]
