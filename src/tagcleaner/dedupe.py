"""Audio-content deduplication via Chromaprint fingerprints.

Detects duplicates by audio content rather than filename. Catches
re-encodes, renames, and lossless/lossy pairs that filename-and-size
tools miss.

Optional dependency: ``pyacoustid`` (the Python wrapper around the
``fpcalc`` binary from libchromaprint). Install with::

    pip install tagcleaner[dedupe]

The ``fpcalc`` binary must also be on PATH:

* Debian/Ubuntu: ``apt install libchromaprint-tools``
* macOS: ``brew install chromaprint``
* Other: https://acoustid.org/chromaprint
"""
from __future__ import annotations

import json
import logging
import shutil as _shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

log = logging.getLogger(__name__)

# Mirror of scanner.AUDIO_EXTS; duplicated here so this module imports without
# pulling in the full parser graph (dateutil etc.) when it's used as a
# standalone fingerprinting helper from server-side scripts.
AUDIO_EXTS = {".flac", ".mp3", ".m4a", ".ogg", ".opus", ".wav", ".aif", ".aiff"}

DEFAULT_FP_THRESHOLD = 0.85
DEFAULT_DURATION_TOL = 7.0
DEFAULT_FOLDER_THRESHOLD = 0.80


def _try_import_acoustid():
    try:
        import acoustid
        return acoustid
    except ImportError:
        return None


def _try_import_chromaprint():
    """Optional. Some platforms have ``acoustid`` (which uses fpcalc directly)
    but not ``libchromaprint``. The byte-Hamming fallback in
    :func:`compare_fingerprints` keeps us working without the library."""
    try:
        import chromaprint
        return chromaprint
    except (ImportError, OSError):
        return None


def _try_import():
    """Backward-compat shim used by the CLI: returns (acoustid_or_None,
    chromaprint_or_None)."""
    return _try_import_acoustid(), _try_import_chromaprint()


def fpcalc_available() -> bool:
    return _shutil.which("fpcalc") is not None


def fingerprint_file(path: Path, length: int = 120) -> tuple[float, str] | None:
    """Run fpcalc on *path*. Returns ``(duration, fingerprint)`` or None.

    Prefers the ``pyacoustid`` Python wrapper when available — it gives
    nicer error types. Falls back to invoking the ``fpcalc`` binary
    directly via subprocess when pyacoustid isn't installable (e.g. on
    constrained Slackware/Unraid setups). Either path needs the
    ``fpcalc`` binary on PATH.
    """
    acoustid = _try_import_acoustid()
    if acoustid is not None:
        try:
            dur, fp = acoustid.fingerprint_file(str(path), maxlength=length)
        except acoustid.FingerprintGenerationError as exc:
            log.debug("fingerprint failed for %s: %s", path, exc)
            return None
        return float(dur), fp.decode("ascii") if isinstance(fp, bytes) else str(fp)
    return _fingerprint_via_fpcalc(path, length)


def _fingerprint_via_fpcalc(path: Path, length: int) -> tuple[float, str] | None:
    import json
    import subprocess
    try:
        result = subprocess.run(
            ["fpcalc", "-json", "-length", str(length), str(path)],
            capture_output=True, timeout=60, check=True, text=True,
        )
    except (FileNotFoundError, subprocess.SubprocessError, subprocess.TimeoutExpired) as exc:
        log.debug("fpcalc failed for %s: %s", path, exc)
        return None
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        log.debug("fpcalc returned non-JSON for %s: %s", path, exc)
        return None
    dur = data.get("duration")
    fp = data.get("fingerprint")
    if dur is None or fp is None:
        return None
    return float(dur), str(fp)


def compare_fingerprints(a_fp: str, b_fp: str) -> float:
    """Inverted bit-error rate. Returns similarity in ``[0, 1]``.

    Prefers the proper Chromaprint decode (via ``libchromaprint``) when
    available — that gives a smooth gradient where slightly-different
    recordings produce slightly-lower scores. When the library isn't
    available (no Slackware/Unraid package, static-fpcalc-only setups),
    falls back to Hamming distance over the raw decoded base64 bytes.

    The fallback is reliable for *identifying duplicates* because Chromaprint
    encoding is deterministic — identical audio produces byte-identical
    fingerprints, so the Hamming-on-bytes score is 1.0 for true matches.
    Genuinely different recordings score around 0.5 (random bits). The
    fallback is less smooth in the middle, so users testing edge cases
    may want to install ``libchromaprint`` for a properly-calibrated
    similarity score.
    """
    chromaprint = _try_import_chromaprint()
    if chromaprint is not None:
        try:
            a_decoded, _ = chromaprint.decode_fingerprint(
                a_fp.encode() if isinstance(a_fp, str) else a_fp
            )
            b_decoded, _ = chromaprint.decode_fingerprint(
                b_fp.encode() if isinstance(b_fp, str) else b_fp
            )
        except Exception:
            pass
        else:
            return _bit_similarity_int32(a_decoded, b_decoded)
    return _bit_similarity_bytes(a_fp, b_fp)


def _bit_similarity_int32(a: Sequence[int], b: Sequence[int]) -> float:
    """Hamming-distance similarity over int32 sequences (libchromaprint path)."""
    if not a or not b:
        return 0.0
    n = min(len(a), len(b))
    diff = 0
    for x, y in zip(a[:n], b[:n]):
        diff += (x ^ y).bit_count()
    return 1.0 - diff / (n * 32)


def _bit_similarity_bytes(a_fp: str, b_fp: str) -> float:
    """Fallback: Hamming over raw base64-decoded bytes. Sufficient for
    identifying duplicates even though it's coarser than the int32 path."""
    import base64
    try:
        a = base64.urlsafe_b64decode(a_fp + "=" * (-len(a_fp) % 4))
        b = base64.urlsafe_b64decode(b_fp + "=" * (-len(b_fp) % 4))
    except Exception:
        return 0.0
    if not a or not b:
        return 0.0
    n = min(len(a), len(b))
    diff = 0
    for x, y in zip(a[:n], b[:n]):
        diff += (x ^ y).bit_count()
    return 1.0 - diff / (n * 8)


# Back-compat alias so existing tests/callers using ``_bit_similarity`` keep working.
_bit_similarity = _bit_similarity_int32


@dataclass
class FingerprintCache:
    """JSON-backed cache keyed on ``str(path)`` with (mtime, size) guard."""

    path: Path | None = None
    entries: dict[str, dict] = field(default_factory=dict)
    dirty: bool = False

    @classmethod
    def load(cls, path: Path | None) -> "FingerprintCache":
        if path is None or not path.exists():
            return cls(path=path)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls(path=path)
        return cls(path=path, entries=data if isinstance(data, dict) else {})

    def save(self) -> None:
        if self.path is None or not self.dirty:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.entries), encoding="utf-8")
            self.dirty = False
        except OSError as exc:
            log.warning("could not save fingerprint cache to %s: %s", self.path, exc)

    def get_or_compute(self, file: Path) -> tuple[float, str] | None:
        try:
            stat = file.stat()
        except OSError:
            return None
        key = str(file)
        cached = self.entries.get(key)
        if (
            cached
            and cached.get("mtime") == stat.st_mtime
            and cached.get("size") == stat.st_size
        ):
            return cached["duration"], cached["fingerprint"]
        result = fingerprint_file(file)
        if result is None:
            return None
        dur, fp = result
        self.entries[key] = {
            "mtime": stat.st_mtime,
            "size": stat.st_size,
            "duration": dur,
            "fingerprint": fp,
        }
        self.dirty = True
        return dur, fp


def iter_audio_files(folder: Path) -> list[Path]:
    return sorted(
        f for f in folder.rglob("*")
        if f.is_file()
        and not f.name.startswith("._")
        and f.suffix.lower() in AUDIO_EXTS
    )


@dataclass
class FolderFingerprint:
    folder: Path
    tracks: list[tuple[Path, float, str]]  # (path, duration, fp_string)

    @property
    def total_size(self) -> int:
        size = 0
        for f, _, _ in self.tracks:
            try:
                size += f.stat().st_size
            except OSError:
                pass
        return size

    @property
    def total_duration(self) -> float:
        return sum(d for _, d, _ in self.tracks)


def fingerprint_folder(
    folder: Path,
    cache: FingerprintCache,
    *,
    on_file: Callable[[Path], None] | None = None,
) -> FolderFingerprint:
    tracks: list[tuple[Path, float, str]] = []
    for f in iter_audio_files(folder):
        if on_file is not None:
            on_file(f)
        result = cache.get_or_compute(f)
        if result is None:
            continue
        dur, fp = result
        tracks.append((f, dur, fp))
    return FolderFingerprint(folder=folder, tracks=tracks)


def folders_match(
    a: FolderFingerprint,
    b: FolderFingerprint,
    *,
    fp_threshold: float = DEFAULT_FP_THRESHOLD,
    duration_tolerance: float = DEFAULT_DURATION_TOL,
    folder_threshold: float = DEFAULT_FOLDER_THRESHOLD,
) -> tuple[bool, float]:
    """Return ``(is_duplicate, fraction_matched)``.

    Tracks are paired by sorted-name index. A pair matches when both
    durations agree within ``duration_tolerance`` and fingerprint
    similarity is at least ``fp_threshold``. The folder verdict is
    ``matches / max(len(a), len(b)) >= folder_threshold`` — matches
    are required against the *longer* of the two so a shorter subset
    can't claim the larger one.
    """
    if not a.tracks or not b.tracks:
        return False, 0.0
    n = min(len(a.tracks), len(b.tracks))
    matches = 0
    for i in range(n):
        _, dur_a, fp_a = a.tracks[i]
        _, dur_b, fp_b = b.tracks[i]
        if abs(dur_a - dur_b) > duration_tolerance:
            continue
        if compare_fingerprints(fp_a, fp_b) >= fp_threshold:
            matches += 1
    longer = max(len(a.tracks), len(b.tracks))
    fraction = matches / longer
    return fraction >= folder_threshold, fraction


def folder_audio_signature(
    folder: Path,
    cache: FingerprintCache | None = None,
) -> str | None:
    """Stable hash over a folder's Chromaprint fingerprints.

    Returns ``None`` when the folder has no fingerprintable audio, or
    when ``fpcalc`` isn't available. The hash is deterministic over
    track fingerprints sorted by file basename, so re-encodes that
    preserve audio content (FLAC compression-level changes, format
    conversions) produce the same signature.

    Designed to be cheap on warm caches: when *cache* already has all
    track fingerprints, this is just a sha1 over them.
    """
    cache = cache or FingerprintCache()
    fp = fingerprint_folder(folder, cache)
    if not fp.tracks:
        return None
    import hashlib
    h = hashlib.sha1()
    for path, _dur, fpstr in sorted(fp.tracks, key=lambda t: t[0].name):
        h.update(path.name.encode("utf-8", "replace"))
        h.update(b"|")
        h.update(fpstr.encode("ascii", "replace"))
        h.update(b"\x00")
    return h.hexdigest()


def are_audio_duplicates(
    folder_a: Path,
    folder_b: Path,
    *,
    cache: FingerprintCache | None = None,
    fp_threshold: float = DEFAULT_FP_THRESHOLD,
    duration_tolerance: float = DEFAULT_DURATION_TOL,
    folder_threshold: float = DEFAULT_FOLDER_THRESHOLD,
) -> tuple[bool, float]:
    """High-level helper: fingerprint two folders and report whether their
    audio content matches.

    Returns ``(is_duplicate, fraction_matched)``. The fraction is over the
    *longer* folder so a short subset can't claim the larger one. Returns
    ``(False, 0.0)`` when either side has no fingerprintable audio.

    The single-folder ``cache`` makes successive same-folder calls cheap;
    pass ``None`` to skip caching entirely (useful for one-off checks).
    """
    cache = cache or FingerprintCache()
    fp_a = fingerprint_folder(folder_a, cache)
    fp_b = fingerprint_folder(folder_b, cache)
    if not fp_a.tracks or not fp_b.tracks:
        return False, 0.0
    return folders_match(
        fp_a, fp_b,
        fp_threshold=fp_threshold,
        duration_tolerance=duration_tolerance,
        folder_threshold=folder_threshold,
    )


def cluster_duplicates(
    fingerprints: Sequence[FolderFingerprint],
    **match_kwargs,
) -> list[list[FolderFingerprint]]:
    """Group folders into duplicate clusters via union-find. Singletons dropped."""
    n = len(fingerprints)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj

    for i in range(n):
        for j in range(i + 1, n):
            is_dupe, _ = folders_match(fingerprints[i], fingerprints[j], **match_kwargs)
            if is_dupe:
                union(i, j)

    clusters: dict[int, list[FolderFingerprint]] = {}
    for i, fp in enumerate(fingerprints):
        clusters.setdefault(find(i), []).append(fp)
    return [c for c in clusters.values() if len(c) > 1]


def pick_keeper(
    cluster: Sequence[FolderFingerprint],
    strategy: str = "largest",
) -> FolderFingerprint:
    """Pick which folder to keep; the others in the cluster are duplicates.

    Strategies:
      * ``largest`` (default) — the folder with the most bytes (proxy for
        highest bitrate / least lossy).
      * ``most-tracks`` — most successfully-fingerprinted tracks.
      * ``oldest`` / ``newest`` — by ``folder.stat().st_mtime``.
    """
    if not cluster:
        raise ValueError("empty cluster")
    if strategy == "largest":
        return max(cluster, key=lambda f: f.total_size)
    if strategy == "most-tracks":
        return max(cluster, key=lambda f: len(f.tracks))
    if strategy in {"oldest", "newest"}:
        def mtime(fp: FolderFingerprint) -> float:
            try:
                return fp.folder.stat().st_mtime
            except OSError:
                return 0.0
        return (min if strategy == "oldest" else max)(cluster, key=mtime)
    raise ValueError(f"unknown keeper strategy: {strategy!r}")
