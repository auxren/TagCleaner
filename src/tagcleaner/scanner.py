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


def list_candidate_dirs(root: Path, *, max_depth: int = 8) -> list[tuple[Path, float]]:
    """Return ``(candidate_folder, folder_mtime)`` pairs beneath *root*.

    Walks recursively so that artist-nested libraries (``Tapes/Artist/show/``)
    or year-nested ones (``Tapes/Artist/1987/show/``) are discovered, not just
    the first level under *root*. A folder is a concert if it either:

      * contains audio files directly, or
      * contains exactly one subdirectory with audio and no other subdirs
        (the classic ``folder/folder/*.flac`` unpack pattern).

    Otherwise we descend into each subdirectory to keep looking.

    ``max_depth`` guards against pathological trees / symlink loops.
    """
    if not root.is_dir():
        return []
    out: list[tuple[Path, float]] = []
    _collect_candidates(root, out, depth=0, max_depth=max_depth)
    out.sort(key=lambda pair: str(pair[0]))
    return out


def _collect_candidates(
    folder: Path,
    out: list[tuple[Path, float]],
    *,
    depth: int,
    max_depth: int,
) -> None:
    if depth > max_depth:
        return
    classified = _classify(folder)
    if classified is None:
        return
    audio, _info, subdirs = classified
    try:
        mtime = folder.stat().st_mtime
    except OSError:
        mtime = 0.0
    if audio:
        out.append((folder, mtime))
        return
    if not subdirs:
        return
    if len(subdirs) == 1 and _looks_like_unpack_wrapper(folder.name, subdirs[0].name):
        inner = _classify(subdirs[0])
        if inner is not None and inner[0]:
            out.append((folder, mtime))
            return
    for sub in subdirs:
        _collect_candidates(sub, out, depth=depth + 1, max_depth=max_depth)


def _looks_like_unpack_wrapper(outer: str, inner: str) -> bool:
    """Decide whether ``outer/inner`` is an archive-unpack wrapper (where the
    inner folder is just a re-run of the outer name, typical of unzipped
    etree torrents) vs. a real container folder (artist, year, etc.) that
    happens to have one child concert.

    We only collapse when the names are effectively the same — otherwise a
    library organised as ``Artist/Year/Show`` would get mis-rooted at the
    year, hiding every other year's shows.
    """
    o = outer.strip().lower()
    i = inner.strip().lower()
    if not o or not i:
        return False
    if o == i:
        return True
    shorter, longer = (o, i) if len(o) <= len(i) else (i, o)
    return len(shorter) >= 6 and longer.startswith(shorter)


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
