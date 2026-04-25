"""Write Vorbis/ID3 tags to audio files. Three modes: dry-run, in-place, copy-to."""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from mutagen.flac import FLAC
from mutagen.easyid3 import EasyID3
from mutagen.id3 import ID3, TALB, TDRC, TIT2, TPE1, TPE2, TPOS, TRCK
from mutagen.mp3 import MP3
from mutagen import File as MutagenFile
from mutagen.mp4 import MP4
from mutagen.oggvorbis import OggVorbis
from mutagen.oggopus import OggOpus
from mutagen.wave import WAVE

from .models import Concert


class Mode(str, Enum):
    DRY_RUN = "dry-run"
    IN_PLACE = "in-place"
    COPY_TO = "copy-to"


@dataclass
class TagPlan:
    file: Path             # source file
    dest: Path             # where tags will land (same as file for in-place)
    artist: str
    album: str
    date: str
    # When None (metadata-only tagging), the per-track fields aren't written
    # and any existing TRACKNUMBER/TITLE/DISC* tags on the file are left alone.
    track: int | None = None
    title: str | None = None
    disc: int | None = None
    disc_total: int | None = None
    # When True, only ARTIST, ALBUMARTIST, ALBUM, and TRACKNUMBER are written;
    # existing DATE/TITLE/DISC tags on the file are left untouched.
    minimal: bool = False


@dataclass
class TagResult:
    plan: TagPlan
    ok: bool
    error: str | None = None
    changed: bool = True       # False when the file was already fully tagged
    album_only: bool = False   # True when only ALBUM was rewritten
    skipped_official: bool = False  # True when file was left untouched
                                    # because it looks like an official release
                                    # (Dick's Picks, Road Trips, MB-tagged, etc.)


def build_plans(
    concert: Concert,
    *,
    copy_to_root: Path | None = None,
    source_root: Path | None = None,
    metadata_only: bool = False,
    minimal: bool = False,
) -> list[TagPlan]:
    """Pair parsed tracks with audio files and produce a TagPlan per file.
    If track/audio counts mismatch we pair by position up to the shorter list;
    the caller is expected to have surfaced the issue already.

    When ``metadata_only`` is True we skip the per-track pairing entirely and
    emit one TagPlan per audio file with only artist/album/date populated.
    Callers use this when a track/audio mismatch is too wide to align safely
    but the concert-level metadata is still worth stamping."""
    if not concert.audio_files:
        return []
    if not metadata_only and not concert.tracks:
        return []
    album = concert.album_name()
    artist = concert.artist or "Unknown Artist"
    date = concert.date or ""
    plans: list[TagPlan] = []

    def _dest(audio: Path) -> Path:
        if copy_to_root is not None and source_root is not None:
            return copy_to_root / audio.relative_to(source_root)
        return audio

    if metadata_only:
        for audio in concert.audio_files:
            plans.append(TagPlan(
                file=audio,
                dest=_dest(audio),
                artist=artist,
                album=album,
                date=date,
                minimal=minimal,
            ))
        return plans

    for audio, track in zip(concert.audio_files, concert.tracks):
        plans.append(TagPlan(
            file=audio,
            dest=_dest(audio),
            artist=artist,
            album=album,
            date=date,
            track=track.number,
            title=track.title,
            disc=track.disc,
            disc_total=track.disc_total,
            minimal=minimal,
        ))
    return plans


def apply_plans(plans: list[TagPlan], mode: Mode) -> list[TagResult]:
    results: list[TagResult] = []
    for plan in plans:
        try:
            if mode is Mode.DRY_RUN:
                results.append(TagResult(plan=plan, ok=True))
                continue
            if mode is Mode.COPY_TO:
                plan.dest.parent.mkdir(parents=True, exist_ok=True)
                if not plan.dest.exists() or plan.dest.stat().st_size != plan.file.stat().st_size:
                    shutil.copy2(plan.file, plan.dest)
            changed, album_only, skipped_official = _write_tags(plan)
            results.append(TagResult(
                plan=plan, ok=True, changed=changed, album_only=album_only,
                skipped_official=skipped_official,
            ))
        except Exception as exc:  # noqa: BLE001 - we want to report every failure
            results.append(TagResult(plan=plan, ok=False, error=f"{type(exc).__name__}: {exc}"))
    return results


def _tag_present(tags, key: str) -> bool:
    """True when *tags* has a non-blank value for *key* (case-insensitive
    for the EasyID3 dict which normalises to lowercase)."""
    val = tags.get(key)
    if val is None:
        return False
    if isinstance(val, list):
        val = val[0] if val else ""
    return bool(str(val).strip())


def _tag_value(tags, *keys) -> str:
    """Return the first non-empty value across *keys*, stripped. '' if none."""
    for key in keys:
        val = tags.get(key)
        if val is None:
            continue
        if isinstance(val, list):
            val = val[0] if val else ""
        s = str(val).strip()
        if s:
            return s
    return ""


def _is_already_tagged(plan: TagPlan, tags) -> bool:
    """True when every field this plan would write (other than ALBUM) is
    already on the file AND matches the plan's value.

    ``tags`` can be any mutagen-style dict (FLAC or EasyID3). ALBUM is
    intentionally excluded — we always want to rewrite it to our
    canonical ``YYYY-MM-DD Venue, City [source]`` format. In ``minimal``
    mode we only check the fields we'd actually write (artist +
    albumartist + track).

    A VALUE-LEVEL check (not just presence) is required: mixed-artist
    folders where one track says "Dinosaur Jr." and the next says
    "Dinosaur Jr., Kevin Sweeney & Kyle Spence" used to be treated as
    "already tagged" and skipped, leaving them inconsistent and landing
    them in Plex's Various Artists bucket.
    """
    existing_artist = _tag_value(tags, "ARTIST", "artist")
    if plan.artist:
        if existing_artist != plan.artist:
            return False
        # Also require ALBUMARTIST to match when we'd write it. In minimal
        # mode we write both ARTIST and ALBUMARTIST — if ALBUMARTIST lags
        # behind (e.g. unchanged from old Plex VA grouping), rewrite.
        existing_aa = _tag_value(tags, "ALBUMARTIST", "albumartist")
        if existing_aa and existing_aa != plan.artist:
            return False
    elif not existing_artist:
        # No plan artist, no existing artist — nothing to enforce.
        return False
    if plan.track is not None:
        existing_track = _tag_value(tags, "TRACKNUMBER", "tracknumber")
        if not existing_track:
            return False
        # Compare as ints (tolerate "1" vs "01" vs "1/12" formats).
        try:
            if int(str(existing_track).split("/")[0]) != plan.track:
                return False
        except (ValueError, TypeError):
            return False
    if plan.minimal:
        return True
    if plan.date and not (_tag_present(tags, "DATE") or _tag_present(tags, "date")):
        return False
    if plan.title is not None and not (
        _tag_present(tags, "TITLE") or _tag_present(tags, "title")
    ):
        return False
    if plan.disc is not None and plan.disc_total is not None and not (
        _tag_present(tags, "DISCNUMBER") or _tag_present(tags, "discnumber")
    ):
        return False
    return True


def _existing_album(tags) -> str:
    for key in ("ALBUM", "album"):
        val = tags.get(key)
        if val is None:
            continue
        if isinstance(val, list):
            val = val[0] if val else ""
        return str(val)
    return ""


# Folder/path tokens that signal a commercial release we should NOT touch:
# Dick's Picks, Road Trips, From the Vault, etc. — Grateful Dead-led but
# kept generic enough to catch other label series that follow the
# "Series Volume N - Date Venue" naming convention.
_OFFICIAL_RELEASE_TOKENS = (
    "dick's picks", "dicks picks", "daves picks", "dave's picks",
    "road trips", "from the vault", "view from the vault",
    "30 trips around the sun", "spring 1990",
    "download series", "complete studio",
    # Other Grateful Dead commercial series.
    "skull and roses", "europe '72", "europe 72",
    "fallout from the phil zone", "fillmore west 1969",
    # Generic commercial markers.
    "official release", "remastered edition",
)

# Description / comment field substrings that strongly signal a
# commercial release. nugs.net is the official Grateful Dead vendor.
_OFFICIAL_DESCRIPTION_MARKERS = (
    "nugs.net", "powered by nugs", "(c) nugs",
    "all rights reserved",
    "amazon music", "itunes",
    "tidal", "qobuz", "deezer",
)


def _tag_first(tags, *keys) -> str:
    """Return the first non-empty tag value across *keys*. Tag containers
    differ by format — keys are tried as-is, with FLAC-style upper and
    EasyID3-style lower case variants implicit via the caller's choices.
    """
    if tags is None:
        return ""
    for key in keys:
        try:
            v = tags.get(key)
        except Exception:
            continue
        if v is None:
            continue
        if isinstance(v, list):
            v = v[0] if v else ""
        s = str(v).strip()
        if s:
            return s
    return ""


def _looks_like_official_release(folder: Path | None, tags) -> bool:
    """True if *folder* / *tags* signal a commercial release we should
    leave alone. Multiple independent signals:

    1. An explicit ``.tagcleaner-skip`` marker file (user override).
    2. Folder path contains a known commercial-series token
       (``Dick's Picks``, ``Road Trips``, ``From the Vault``, …).
    3. ``MUSICBRAINZ_RELEASETRACKID`` is set, OR
       ``MUSICBRAINZ_ALBUMSTATUS == 'Official'``.
    4. The DESCRIPTION / COMMENT tag contains a vendor signature
       (``nugs.net``, ``iTunes``, ``Amazon Music``, …) — strong evidence
       the file came from a commercial source.

    *tags* may be None (when called pre-open) or any dict-like tag
    container — FLAC, EasyID3, _Id3View, _Mp4View all qualify.
    """
    if folder is not None:
        try:
            if (folder / ".tagcleaner-skip").exists():
                return True
        except OSError:
            pass
        pathstr = str(folder).lower()
        if any(tok in pathstr for tok in _OFFICIAL_RELEASE_TOKENS):
            return True
    if tags is not None:
        try:
            keys_upper = {str(k).upper() for k in tags.keys()}
        except Exception:
            keys_upper = set()
        if "MUSICBRAINZ_RELEASETRACKID" in keys_upper:
            return True
        status = _tag_first(tags, "MUSICBRAINZ_ALBUMSTATUS", "musicbrainz_albumstatus")
        if status and status.lower() == "official":
            return True
        # Vendor signature in description / comment.
        desc = _tag_first(
            tags, "DESCRIPTION", "description", "COMMENT", "comment"
        ).lower()
        if desc and any(m in desc for m in _OFFICIAL_DESCRIPTION_MARKERS):
            return True
    return False


def _write_tags(plan: TagPlan) -> tuple[bool, bool, bool]:
    """Apply *plan* to the destination file.

    Returns ``(changed, album_only, skipped_official)``:
    * ``changed`` — whether the file was actually saved.
    * ``album_only`` — True when every plan field except ALBUM was already
      populated, so we only rewrote ALBUM to match the canonical format.
    * ``skipped_official`` — True when the file was left untouched
      because it looks like a commercial release (Dick's Picks, Road
      Trips, MusicBrainz-tagged release, ``.tagcleaner-skip`` marker).

    When the file is already fully tagged AND its ALBUM already equals
    our planned album, the file is left untouched.
    """
    ext = plan.dest.suffix.lower()
    # Path-level skip first (cheap — no file open required).
    if _looks_like_official_release(plan.dest.parent, None):
        return False, False, True
    if ext == ".flac":
        audio = FLAC(str(plan.dest))
        if _looks_like_official_release(plan.dest.parent, audio):
            return False, False, True
        if _is_already_tagged(plan, audio):
            if _existing_album(audio) == plan.album:
                return False, True, False
            audio["ALBUM"] = plan.album
            audio.save()
            return True, True, False
        audio["ARTIST"] = plan.artist
        audio["ALBUMARTIST"] = plan.artist
        audio["ALBUM"] = plan.album
        if plan.track is not None:
            audio["TRACKNUMBER"] = f"{plan.track:02d}"
        if not plan.minimal:
            if plan.date:
                audio["DATE"] = plan.date
            if plan.title is not None:
                audio["TITLE"] = plan.title
            if plan.disc is not None and plan.disc_total is not None:
                audio["DISCNUMBER"] = str(plan.disc)
                audio["DISCTOTAL"] = str(plan.disc_total)
            elif plan.track is not None:
                # Only scrub disc tags when we're writing a full per-track plan.
                # Metadata-only plans leave any existing disc tags alone.
                for k in ("DISCNUMBER", "DISCTOTAL"):
                    if k in audio:
                        del audio[k]
        audio.save()
        return True, False, False
    if ext == ".mp3":
        try:
            audio = EasyID3(str(plan.dest))
        except Exception:
            mp3 = MP3(str(plan.dest))
            mp3.add_tags()
            mp3.save()
            audio = EasyID3(str(plan.dest))
        if _looks_like_official_release(plan.dest.parent, audio):
            return False, False, True
        if _is_already_tagged(plan, audio):
            if _existing_album(audio) == plan.album:
                return False, True, False
            audio["album"] = plan.album
            audio.save()
            return True, True, False
        audio["artist"] = plan.artist
        audio["albumartist"] = plan.artist
        audio["album"] = plan.album
        if plan.track is not None:
            audio["tracknumber"] = f"{plan.track:02d}"
        if not plan.minimal:
            if plan.date:
                audio["date"] = plan.date
            if plan.title is not None:
                audio["title"] = plan.title
            if plan.disc is not None and plan.disc_total is not None:
                audio["discnumber"] = f"{plan.disc}/{plan.disc_total}"
        audio.save()
        return True, False, False
    if ext in (".wav", ".wave"):
        audio = WAVE(str(plan.dest))
        if audio.tags is None:
            audio.add_tags()
        if _looks_like_official_release(plan.dest.parent, audio.tags):
            return False, False, True
        view = _Id3View(audio.tags)
        if _is_already_tagged(plan, view):
            if _existing_album(view) == plan.album:
                return False, True, False
            audio.tags["TALB"] = TALB(encoding=3, text=plan.album)
            audio.save()
            return True, True, False
        audio.tags["TPE1"] = TPE1(encoding=3, text=plan.artist)
        audio.tags["TPE2"] = TPE2(encoding=3, text=plan.artist)
        audio.tags["TALB"] = TALB(encoding=3, text=plan.album)
        if plan.track is not None:
            audio.tags["TRCK"] = TRCK(encoding=3, text=f"{plan.track:02d}")
        if not plan.minimal:
            if plan.date:
                audio.tags["TDRC"] = TDRC(encoding=3, text=plan.date)
            if plan.title is not None:
                audio.tags["TIT2"] = TIT2(encoding=3, text=plan.title)
            if plan.disc is not None and plan.disc_total is not None:
                audio.tags["TPOS"] = TPOS(encoding=3, text=f"{plan.disc}/{plan.disc_total}")
            elif plan.track is not None and "TPOS" in audio.tags:
                del audio.tags["TPOS"]
        audio.save()
        return True, False, False
    if ext == ".m4a" or ext == ".m4b":
        audio = MP4(str(plan.dest))
        if audio.tags is None:
            audio.add_tags()
        if _looks_like_official_release(plan.dest.parent, audio.tags):
            return False, False, True
        view = _Mp4View(audio.tags)
        if _is_already_tagged(plan, view):
            if _existing_album(view) == plan.album:
                return False, True, False
            audio.tags["\xa9alb"] = [plan.album]
            audio.save()
            return True, True, False
        audio.tags["\xa9ART"] = [plan.artist]
        audio.tags["aART"] = [plan.artist]
        audio.tags["\xa9alb"] = [plan.album]
        if plan.track is not None:
            audio.tags["trkn"] = [(plan.track, 0)]
        if not plan.minimal:
            if plan.date:
                audio.tags["\xa9day"] = [plan.date]
            if plan.title is not None:
                audio.tags["\xa9nam"] = [plan.title]
            if plan.disc is not None and plan.disc_total is not None:
                audio.tags["disk"] = [(plan.disc, plan.disc_total)]
            elif plan.track is not None and "disk" in audio.tags:
                del audio.tags["disk"]
        audio.save()
        return True, False, False
    if ext in (".ogg", ".opus", ".oga"):
        # .ogg can hold Vorbis or Opus (or FLAC) — let mutagen sniff it.
        # All Ogg variants expose Vorbis comments via dict-style access.
        audio = MutagenFile(str(plan.dest))
        if audio is None or not isinstance(audio, (OggVorbis, OggOpus)):
            raise RuntimeError(f"unsupported ogg variant for {plan.dest}")
        if _looks_like_official_release(plan.dest.parent, audio):
            return False, False, True
        if _is_already_tagged(plan, audio):
            if _existing_album(audio) == plan.album:
                return False, True, False
            audio["ALBUM"] = plan.album
            audio.save()
            return True, True, False
        audio["ARTIST"] = plan.artist
        audio["ALBUMARTIST"] = plan.artist
        audio["ALBUM"] = plan.album
        if plan.track is not None:
            audio["TRACKNUMBER"] = f"{plan.track:02d}"
        if not plan.minimal:
            if plan.date:
                audio["DATE"] = plan.date
            if plan.title is not None:
                audio["TITLE"] = plan.title
            if plan.disc is not None and plan.disc_total is not None:
                audio["DISCNUMBER"] = str(plan.disc)
                audio["DISCTOTAL"] = str(plan.disc_total)
            elif plan.track is not None:
                for k in ("DISCNUMBER", "DISCTOTAL"):
                    if k in audio:
                        del audio[k]
        audio.save()
        return True, False, False
    raise RuntimeError(f"unsupported audio format: {ext}")


# MP4/iTunes atom name -> EasyID3-style aliases probed by _is_already_tagged.
# Lets M4A files reuse the same already-tagged + existing-album checks.
_MP4_ATOM_FOR_KEY = {
    "ARTIST": "\xa9ART", "artist": "\xa9ART", "ARTISTS": "\xa9ART",
    "ALBUMARTIST": "aART", "albumartist": "aART",
    "ALBUM": "\xa9alb", "album": "\xa9alb",
    "TRACKNUMBER": "trkn", "tracknumber": "trkn",
    "DATE": "\xa9day", "date": "\xa9day",
    "TITLE": "\xa9nam", "title": "\xa9nam",
    "DISCNUMBER": "disk", "discnumber": "disk",
}


class _Mp4View:
    """Dict-style read-only adapter exposing MP4 atoms under EasyID3-style
    keys, so ``_is_already_tagged`` and ``_existing_album`` work uniformly
    across FLAC, MP3, WAV, and M4A."""

    __slots__ = ("_mp4",)

    def __init__(self, mp4_tags) -> None:
        self._mp4 = mp4_tags

    def get(self, key: str, default=None):
        atom = _MP4_ATOM_FOR_KEY.get(key)
        if atom is None:
            return default
        v = self._mp4.get(atom)
        if not v:
            return default
        # `trkn` is [(num, total)] tuples; `disk` similar. Stringify the first.
        first = v[0]
        if isinstance(first, tuple):
            return [str(first[0])]
        return [str(first)]


# ID3 frame name -> the EasyID3-style aliases that _is_already_tagged probes.
# Lets WAV files (which carry raw ID3 frames in their tags chunk) reuse the
# same already-tagged + existing-album checks as FLAC and MP3.
_ID3_FRAME_FOR_KEY = {
    "ARTIST": "TPE1", "artist": "TPE1",
    "ARTISTS": "TPE1", "ALBUMARTIST": "TPE2", "albumartist": "TPE2",
    "ALBUM": "TALB", "album": "TALB",
    "TRACKNUMBER": "TRCK", "tracknumber": "TRCK",
    "DATE": "TDRC", "date": "TDRC",
    "TITLE": "TIT2", "title": "TIT2",
    "DISCNUMBER": "TPOS", "discnumber": "TPOS",
    "DISCTOTAL": "TPOS",
}


class _Id3View:
    """Tiny dict-style read-only adapter exposing ID3 frames under
    EasyID3/Vorbis-style keys, so ``_is_already_tagged`` and
    ``_existing_album`` work uniformly across FLAC, MP3, and WAV."""

    __slots__ = ("_id3",)

    def __init__(self, id3: ID3) -> None:
        self._id3 = id3

    def get(self, key: str, default=None):
        frame_id = _ID3_FRAME_FOR_KEY.get(key)
        if frame_id is None:
            return default
        frame = self._id3.get(frame_id)
        if frame is None:
            return default
        return [str(t) for t in frame.text] if frame.text else default
