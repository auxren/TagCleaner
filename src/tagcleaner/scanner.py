"""Find concert folders beneath a root directory.

A "concert folder" is any directory that directly contains audio files, or
whose single subdirectory does (a common pattern where folders are named once
and the extracted archive nests the same name inside), or whose subdirectories
are all disc-named (Disc 1, CD 2, d1, ...) and hold the per-disc audio of a
single multi-disc show. We treat each such folder as a candidate and pair it
with the largest non-fingerprint .txt file inside for parser input.

Performance note: each candidate is enumerated with a single ``os.scandir``
pass. That sweep classifies audio/txt/subdirs *and* produces the content
fingerprint from cached ``DirEntry.stat()`` results — no second round-trip to
the filesystem per file. On slow network shares that's the difference between
minutes and seconds per folder.
"""
from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Callable, Optional

from .parser import build_concert
from .models import Concert

AUDIO_EXTS = {".flac", ".mp3", ".m4a", ".ogg", ".opus", ".wav"}
FINGERPRINT_EXT_HINTS = ("ffp", "md5", "sha", "shntool", "audiochecker", "sbeok")

# Folder-name-only disc marker: matches 'Disc 1', 'CD 2', 'cd2', 'd1',
# 'disc_02', 'Disc One', 'Set 1', 'Encore', 'Early Show', etc. The trailing
# ``\s*$`` anchor is what makes this safe -- 'Disc 2' matches but
# '1984-09-21 Disc 2' does not, because the whole name must be the marker.
DISC_FOLDER_RE = re.compile(
    r"^\s*"
    r"(?:"
    r"(?:disc|disk|cd|d)\s*[_-]?\s*\d{1,2}"
    r"|(?:disc|disk|cd)\s*[_-]?\s*(?:one|two|three|four|five|six|seven|eight)"
    r"|set\s*[_-]?\s*(?:\d{1,2}|one|two|three|four|five|i{1,3}|iv|v)"
    r"|encore|early\s*show|late\s*show|matinee(?:\s+show)?|evening(?:\s+show)?"
    r")"
    r"\s*$",
    re.I,
)

# Map a disc-folder name to an integer for ordering. "Disc 10" must sort after
# "Disc 2", which string sort gets wrong. Returns ``None`` for non-numeric
# markers (Encore, Early Show) so they sort last by name.
_DISC_NUM_RE = re.compile(r"(\d{1,2})")
_DISC_WORD = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8}


def _disc_sort_key(name: str) -> tuple[int, str]:
    low = name.lower()
    m = _DISC_NUM_RE.search(low)
    if m:
        return (int(m.group(1)), low)
    for word, n in _DISC_WORD.items():
        if word in low:
            return (n, low)
    # Encore / late show / etc. -- push to the end but keep name-order stable.
    return (999, low)


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
    if _is_multi_disc_parent(subdirs):
        out.append((folder, mtime))
        return
    for sub in subdirs:
        _collect_candidates(sub, out, depth=depth + 1, max_depth=max_depth)


def _is_multi_disc_parent(subdirs: list[Path]) -> bool:
    """True when *subdirs* is a set of 2+ disc-named folders each containing
    audio. Used to treat ``show/Disc 1/*.flac`` + ``show/Disc 2/*.flac`` as a
    single multi-disc concert rooted at ``show``. Any non-disc-named subdir
    disqualifies the whole parent -- mixed layouts fall back to per-subdir
    descent so nothing gets hidden."""
    if len(subdirs) < 2:
        return False
    for sub in subdirs:
        if not DISC_FOLDER_RE.match(sub.name):
            return False
        inner = _classify(sub)
        if inner is None or not inner[0]:
            return False
    return True


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
        fp = _fingerprint(folder.name, audio, info, rel_to=folder)
        return folder, [p for p, _ in audio], (info[0][0] if info else None), fp
    if _is_multi_disc_parent(subdirs):
        all_audio: list[tuple[Path, int]] = []
        merged_info: list[tuple[Path, int]] = list(info)
        for disc_dir in sorted(subdirs, key=lambda p: _disc_sort_key(p.name)):
            inner = _classify(disc_dir)
            if inner is None:
                continue
            inner_audio, inner_info, _ = inner
            # Preserve disc order, then track order within each disc.
            all_audio.extend(sorted(inner_audio, key=lambda pair: pair[0].name))
            if not merged_info and inner_info:
                merged_info = inner_info
        if all_audio:
            fp = _fingerprint(folder.name, all_audio, merged_info, rel_to=folder)
            return (
                folder,
                [p for p, _ in all_audio],
                (merged_info[0][0] if merged_info else None),
                fp,
            )
    for child in subdirs:
        inner = _classify(child)
        if inner is None:
            continue
        inner_audio, inner_info, _ = inner
        if not inner_audio:
            continue
        fp = _fingerprint(folder.name, inner_audio, inner_info, rel_to=child)
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
    *,
    rel_to: Path | None = None,
) -> str:
    """Cheap content hash built inline from cached ``DirEntry.stat`` sizes.

    Matches the old ``history.fingerprint`` scheme: folder name + sorted
    (audio name, size) + (info.txt name, size). Rename the folder, add /
    remove / resize any audio file, or swap the info.txt and the fingerprint
    changes. File bodies are deliberately not hashed — opening every file
    would defeat the point of skipping.

    When *rel_to* is given, each audio file's key is its path relative to that
    folder (e.g. ``Disc 1/01.flac``) rather than just the basename. This keeps
    the fingerprint unique across multi-disc shows where every disc has a
    ``01.flac``. For single-folder shows the relative path equals the basename
    so old fingerprints stay stable.
    """
    def _key(p: Path) -> str:
        if rel_to is not None:
            try:
                return str(p.relative_to(rel_to))
            except ValueError:
                return p.name
        return p.name

    h = hashlib.sha1()
    h.update(folder_name.encode("utf-8", "replace"))
    for p, size in sorted(audio_pairs, key=lambda pair: _key(pair[0])):
        h.update(b"\x00a:")
        h.update(_key(p).encode("utf-8", "replace"))
        h.update(b"|")
        h.update(str(size).encode("ascii"))
    if info_pairs:
        first_path, first_size = info_pairs[0]
        h.update(b"\x00i:")
        h.update(first_path.name.encode("utf-8", "replace"))
        h.update(b"|")
        h.update(str(first_size).encode("ascii"))
    return h.hexdigest()
