"""Find concert folders beneath a root directory.

A "concert folder" is any directory that directly contains audio files, or
whose single subdirectory does (a common pattern where folders are named once
and the extracted archive nests the same name inside). We treat each such
folder as a candidate and pair it with the largest non-fingerprint .txt file
inside for parser input.

Performance note: each candidate is enumerated with a single ``os.scandir``
pass. That sweep classifies audio/txt/subdirs *and* produces the content
fingerprint from cached ``DirEntry.stat()`` results — no second round-trip to
the filesystem per file. On slow network shares that's the difference between
minutes and seconds per folder.
"""
from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Callable, Optional

from .parser import build_concert
from .models import Concert

AUDIO_EXTS = {".flac", ".mp3", ".m4a", ".ogg", ".opus", ".wav"}
FINGERPRINT_EXT_HINTS = ("ffp", "md5", "sha", "shntool", "audiochecker", "sbeok")


def iter_concert_folders(root: Path) -> Iterator[tuple[Path, list[Path], Path | None]]:
    """Yield (folder, audio_files, info_txt) for each concert-like folder."""
    for folder, _mtime in list_candidate_dirs(root):
        enum = _enumerate_folder(folder)
        if enum is None:
            continue
        _host, audio, info, _fp = enum
        yield folder, audio, info


def list_candidate_dirs(root: Path) -> list[tuple[Path, float]]:
    """Return ``(candidate_folder, folder_mtime)`` pairs beneath *root*.

    Uses ``os.scandir`` so the mtime comes from the cached ``DirEntry.stat``
    (one syscall per candidate, not two). The CLI uses the mtime to decide
    whether to skip enumeration entirely via history.
    """
    if not root.is_dir():
        return []
    out: list[tuple[Path, float]] = []
    try:
        with os.scandir(root) as it:
            for entry in it:
                if entry.name.startswith("."):
                    continue
                try:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                out.append((Path(entry.path), st.st_mtime))
    except OSError:
        return []
    out.sort(key=lambda pair: pair[0].name)
    return out


def scan(
    root: Path,
    *,
    pre_skip: Callable[[Path, float], bool] | None = None,
    skip: Callable[[Path, str], bool] | None = None,
    on_folder: Callable[[Path, int, int], None] | None = None,
    on_skip: Callable[[Path, int, int], None] | None = None,
    on_done: Callable[[Concert, int, int], None] | None = None,
) -> list[tuple[Concert, str, float]]:
    """Parse every concert folder under *root*.

    Returns a list of ``(concert, fingerprint, folder_mtime)`` triples so the
    CLI can record them in history without re-stating the files.

    Callbacks (all optional):
      * ``pre_skip(folder, mtime) -> bool`` — fires BEFORE enumeration. If it
        returns True the folder is skipped without ever being opened. Used by
        the CLI to skip folders whose mtime matches a tagged history entry.
      * ``skip(folder, fingerprint) -> bool`` — fires after enumeration once
        the fingerprint is known, so the caller can confirm skip-eligibility
        against a stored fingerprint.
      * ``on_folder(path, index, total)`` — fires before parse.
      * ``on_skip(path, index, total)`` — fires for either skip mechanism.
      * ``on_done(concert, index, total)`` — fires after parse.

    All callbacks receive 1-based indices and the total candidate count.
    """
    candidates = list_candidate_dirs(root)
    total = len(candidates)
    results: list[tuple[Concert, str, float]] = []
    for idx, (folder, mtime) in enumerate(candidates, start=1):
        if pre_skip is not None and pre_skip(folder, mtime):
            if on_skip is not None:
                on_skip(folder, idx, total)
            continue
        enum = _enumerate_folder(folder)
        if enum is None:
            if on_folder is not None:
                on_folder(folder, idx, total)
            continue
        _host, audio, info, fp = enum
        if skip is not None and skip(folder, fp):
            if on_skip is not None:
                on_skip(folder, idx, total)
            continue
        if on_folder is not None:
            on_folder(folder, idx, total)
        concert = build_concert(folder, audio, info)
        results.append((concert, fp, mtime))
        if on_done is not None:
            on_done(concert, idx, total)
    return results


def _enumerate_folder(
    folder: Path,
) -> Optional[tuple[Path, list[Path], Optional[Path], str]]:
    """One-scandir classification of *folder*.

    Returns ``(host_folder, audio_files, info_txt, fingerprint)`` or None if
    the folder has no audio. ``host_folder`` is ``folder`` itself unless audio
    lives one level deeper (the nested "folder/folder/*.flac" pattern), in
    which case it's the inner directory. The fingerprint is computed inline
    from cached ``DirEntry.stat`` sizes — no second round-trip.
    """
    classified = _classify(folder)
    if classified is None:
        return None
    audio, info, subdirs = classified
    if audio:
        fp = _fingerprint(folder.name, audio, info)
        return folder, [p for p, _ in audio], (info[0][0] if info else None), fp
    for child in subdirs:
        inner = _classify(child)
        if inner is None:
            continue
        inner_audio, inner_info, _ = inner
        if not inner_audio:
            continue
        fp = _fingerprint(folder.name, inner_audio, inner_info)
        return (
            child,
            [p for p, _ in inner_audio],
            (inner_info[0][0] if inner_info else None),
            fp,
        )
    return None


def _classify(
    folder: Path,
) -> Optional[tuple[list[tuple[Path, int]], list[tuple[Path, int]], list[Path]]]:
    """Single ``os.scandir`` sweep. Returns ``(audio, info, subdirs)``.

    * ``audio`` — ``(Path, size)`` pairs sorted by filename.
    * ``info`` — ``(Path, size)`` pairs sorted largest-first (drops any
      filename hinting at a fingerprint / checksum manifest).
    * ``subdirs`` — child directories, sorted by name.

    Sizes come from ``DirEntry.stat()`` which reuses the stat cached during
    directory iteration on most filesystems — no extra syscall per file.
    """
    audio: list[tuple[Path, int]] = []
    info: list[tuple[Path, int]] = []
    subdirs: list[Path] = []
    try:
        with os.scandir(folder) as it:
            for entry in it:
                name = entry.name
                if name.startswith("."):
                    continue
                try:
                    is_file = entry.is_file(follow_symlinks=False)
                    is_dir = entry.is_dir(follow_symlinks=False)
                except OSError:
                    continue
                if is_file:
                    ext = os.path.splitext(name)[1].lower()
                    if ext in AUDIO_EXTS:
                        audio.append((Path(entry.path), _entry_size(entry)))
                    elif ext == ".txt":
                        low = name.lower()
                        if any(h in low for h in FINGERPRINT_EXT_HINTS):
                            continue
                        info.append((Path(entry.path), _entry_size(entry)))
                elif is_dir:
                    subdirs.append(Path(entry.path))
    except OSError:
        return None
    audio.sort(key=lambda pair: pair[0].name)
    info.sort(key=lambda pair: -pair[1])
    subdirs.sort(key=lambda p: p.name)
    return audio, info, subdirs


def _entry_size(entry: "os.DirEntry[str]") -> int:
    try:
        return entry.stat(follow_symlinks=False).st_size
    except OSError:
        return -1


def _fingerprint(
    folder_name: str,
    audio_pairs: list[tuple[Path, int]],
    info_pairs: list[tuple[Path, int]],
) -> str:
    """Cheap content hash built inline from cached ``DirEntry.stat`` sizes.

    Matches the old ``history.fingerprint`` scheme: folder name + sorted
    (audio name, size) + (info.txt name, size). Rename the folder, add /
    remove / resize any audio file, or swap the info.txt and the fingerprint
    changes. File bodies are deliberately not hashed — opening every file
    would defeat the point of skipping.
    """
    h = hashlib.sha1()
    h.update(folder_name.encode("utf-8", "replace"))
    for p, size in sorted(audio_pairs, key=lambda pair: pair[0].name):
        h.update(b"\x00a:")
        h.update(p.name.encode("utf-8", "replace"))
        h.update(b"|")
        h.update(str(size).encode("ascii"))
    if info_pairs:
        first_path, first_size = info_pairs[0]
        h.update(b"\x00i:")
        h.update(first_path.name.encode("utf-8", "replace"))
        h.update(b"|")
        h.update(str(first_size).encode("ascii"))
    return h.hexdigest()
