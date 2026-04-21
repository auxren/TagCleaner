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

# Disc-marker grammar shared by DISC_FOLDER_RE (whole-name) and
# _DISC_TOKEN_RE (anywhere-in-name). Covers: 'Disc 1', 'CD 2', 'cd2', 'd1',
# 'disc_02', 'Disc One', 'DVD 1', 'Set 1', 'Set II', '1st Set', 'first set',
# 'Volume 4', 'Vol 2', 'Encore', 'Intermission Set', 'Early Show'.
_DISC_TOKEN_BODY = r"""
(?:
    \b(?:disc|disk|dvd|cd)\s*[_.\-]?\s*
        (\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten)\b
  | (?:^|(?<=[\s_\-]))d\s*(\d{1,2})\b
  | \bset\s*[_.\-]?\s*
        (\d{1,2}|i{1,3}|iv|v|one|two|three|four|five)\b
  | \b(\d+)(?:st|nd|rd|th)\s+set\b
  | \b(first|second|third|fourth|fifth|sixth|seventh|eighth)\s+set\b
  | \bvol(?:ume)?\s*[_.\-]?\s*
        (\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten)\b
  | \b(encore|intermission(?:\s+set)?
       |matinee(?:\s+show)?|early\s+show|late\s+show|evening(?:\s+show)?)\b
)
"""

# Anywhere-in-name marker: matches 'Disc 1' inside 'WHEN WE WERE KINGS [Disc 1]'
# or '16Bit (Disc.2)'. Used by _parse_disc_marker to extract the base text
# outside the marker for shared-prefix multi-disc detection.
_DISC_TOKEN_RE = re.compile(_DISC_TOKEN_BODY, re.I | re.VERBOSE)

# Whole-name disc marker: 'Disc 2' matches, '1984-09-21 Disc 2' does not.
# Kept as a public helper for callers that need a strict yes/no on the name
# alone.
DISC_FOLDER_RE = re.compile(r"^\s*" + _DISC_TOKEN_BODY + r"\s*$", re.I | re.VERBOSE)

# Names that are clearly format-bucket wrappers, not real concert folders.
# Used to collapse parent/{FLAC,MP3,CD,...}/audio.flac into the parent.
_FORMAT_WRAPPER_NAMES = frozenset({
    "flac", "flac16", "flac24", "flac1644", "flac2448",
    "mp3", "wav", "shn", "shnf",
    "audio", "files", "music", "tracks", "songs",
    "cd", "single cd", "compact disc",
})

# Trailing disc-index suffix used by _shared_prefix_disc_set to recognise
# sets like "BOOKER T 1" / "BOOKER T 2" or bare "1" / "2".
_PREFIX_DISC_SUFFIX_RE = re.compile(
    r"^(.*?)[\s_\-]*(\d{1,2}|one|two|three|four|five|six|seven|eight)\s*$",
    re.I,
)

# Word/ordinal/roman-numeral form of a disc index, mapped to its integer.
_DISC_WORD_NUM = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
    "sixth": 6, "seventh": 7, "eighth": 8,
    "i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5,
}

# Non-numeric set markers ordered relative to numbered discs.
_LATE_MARKER_NUM = {
    "encore": 99, "intermission": 99, "intermission set": 99,
    "late show": 99, "evening": 99, "evening show": 99,
    "matinee": 0, "matinee show": 0, "early show": 0,
}

_DISC_NUM_RE = re.compile(r"(\d{1,2})")


def _parse_disc_marker(name: str) -> tuple[str, int] | None:
    """Find the first disc marker in *name* and return ``(base, idx)``.

    *base* is the rest of the name after the marker (and any wrapping
    brackets/parens/dots) is stripped, lowercased and whitespace-collapsed.
    *idx* is an integer suitable for sorting (encore/intermission → 99,
    matinee/early show → 0). Returns ``None`` when no marker is found.

    Examples
    --------
    >>> _parse_disc_marker("Disc 1")
    ('', 1)
    >>> _parse_disc_marker("WHEN WE WERE KINGS [Disc 2]")
    ('when we were kings', 2)
    >>> _parse_disc_marker("Hello Old Friend, Van (Disc 1)")
    ("hello old friend, van", 1)
    >>> _parse_disc_marker("Volume 4")
    ('', 4)
    >>> _parse_disc_marker("1st Set")
    ('', 1)
    """
    m = _DISC_TOKEN_RE.search(name)
    if m is None:
        return None
    raw = next((g for g in m.groups() if g is not None), "")
    raw_low = raw.lower().strip()
    if raw_low.isdigit():
        idx = int(raw_low)
    elif raw_low in _DISC_WORD_NUM:
        idx = _DISC_WORD_NUM[raw_low]
    elif raw_low in _LATE_MARKER_NUM:
        idx = _LATE_MARKER_NUM[raw_low]
    else:
        idx = 99
    base_raw = name[: m.start()] + name[m.end() :]
    base = re.sub(r"[\[\](){}]", " ", base_raw)
    base = re.sub(r"[\s_\-.]+", " ", base).strip().lower()
    return base, idx


def _disc_sort_key(name: str) -> tuple[int, str]:
    parsed = _parse_disc_marker(name)
    if parsed is not None:
        return (parsed[1], name.lower())
    low = name.lower()
    m = _DISC_NUM_RE.search(low)
    if m:
        return (int(m.group(1)), low)
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
        # A folder with audio is usually a concert, but it can also be a
        # library root where someone left stray loose files next to organised
        # show subfolders. Keep descending so those nested shows aren't
        # hidden; descent is a no-op for concerts whose only children are
        # accessory dirs like ``Artwork``.
        for sub in subdirs:
            _collect_candidates(sub, out, depth=depth + 1, max_depth=max_depth)
        return
    if not subdirs:
        return
    if len(subdirs) == 1 and _looks_like_unpack_wrapper(folder.name, subdirs[0].name):
        inner = _classify(subdirs[0])
        if inner is not None and inner[0]:
            out.append((folder, mtime))
            return
    if _is_multi_disc_parent(folder, subdirs):
        out.append((folder, mtime))
        return
    for sub in subdirs:
        _collect_candidates(sub, out, depth=depth + 1, max_depth=max_depth)


def _is_multi_disc_parent(parent: Path, subdirs: list[Path]) -> bool:
    """True when *subdirs* hold a single multi-disc concert.

    The parent qualifies when 2+ audio-bearing children are all disc-shaped.
    Two strategies are tried, in order:

    * **Strict marker** — every name contains a recognised disc keyword
      (Disc, CD, DVD, Set, Vol, 1st Set, Encore, ...). The text *outside*
      the marker (its "base") must be the same for every child, and the
      marker indices must be unique. This catches bracketed forms like
      ``WHEN WE WERE KINGS [Disc 1]`` / ``[Disc 2]`` and trailing-format
      forms like ``Disc 1 Flac`` / ``Disc 2 Flac``.
    * **Loose shared prefix** — names share a common prefix that differs only
      by a trailing index (BOOKER T 1 / 2, bare 1 / 2). This branch
      additionally requires the parent to look like a concert (date in name
      or info.txt present), so sibling artist folders
      ``show1``/``show2``/``show3`` at a library root don't false-collapse.

    Non-audio peers (Artwork, Scans) are tolerated and ignored. Any
    audio-bearing peer that is *not* disc-shaped disqualifies the rollup so
    standalone shows accidentally placed alongside discs aren't hidden.
    """
    audio_subs: list[Path] = []
    parent_classified = _classify(parent)
    parent_info = bool(parent_classified and parent_classified[1])
    for sub in subdirs:
        inner = _classify(sub)
        if inner is not None and inner[0]:
            audio_subs.append(sub)
    if len(audio_subs) < 2:
        return False
    sigs = [_parse_disc_marker(s.name.strip()) for s in audio_subs]
    if all(sig is not None for sig in sigs):
        bases = {sig[0] for sig in sigs}
        indices = [sig[1] for sig in sigs]
        if len(bases) == 1 and len(set(indices)) == len(indices):
            return True
    if _shared_prefix_disc_set(audio_subs):
        return parent_info or _name_has_date(parent.name)
    return False


def _name_has_date(name: str) -> bool:
    """Cheap date-shape check used as a concert-folder signal. Matches
    YYYY-MM-DD, YYYY.MM.DD, YYYYMMDD, YY-MM-DD."""
    return bool(re.search(r"(?:19|20)?\d{2}[-._]?\d{2}[-._]?\d{2}", name))


def _shared_prefix_disc_set(subdirs: list[Path]) -> bool:
    """True when every name shares a common prefix and differs only by a
    trailing disc index. Catches sibling sets the strict regex misses:
    ``BOOKER T 1`` / ``BOOKER T 2``, ``1`` / ``2``, ``Acoustic 1`` /
    ``Acoustic 2``."""
    if len(subdirs) < 2:
        return False
    bases: list[str] = []
    suffixes: list[str] = []
    for sub in subdirs:
        m = _PREFIX_DISC_SUFFIX_RE.match(sub.name.strip())
        if not m:
            return False
        bases.append(m.group(1).strip().rstrip(" -_").lower())
        suffixes.append(m.group(2).lower())
    if len(set(suffixes)) != len(suffixes):
        return False
    return all(b == bases[0] for b in bases)


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
    if i in _FORMAT_WRAPPER_NAMES:
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
    if _is_multi_disc_parent(folder, subdirs):
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
