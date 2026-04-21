"""Write Vorbis/ID3 tags to audio files. Three modes: dry-run, in-place, copy-to."""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from mutagen.flac import FLAC
from mutagen.easyid3 import EasyID3
from mutagen.mp3 import MP3

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


@dataclass
class TagResult:
    plan: TagPlan
    ok: bool
    error: str | None = None
    changed: bool = True       # False when the file was already fully tagged
    album_only: bool = False   # True when only ALBUM was rewritten


def build_plans(
    concert: Concert,
    *,
    copy_to_root: Path | None = None,
    source_root: Path | None = None,
    metadata_only: bool = False,
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
            changed, album_only = _write_tags(plan)
            results.append(TagResult(
                plan=plan, ok=True, changed=changed, album_only=album_only,
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


def _is_already_tagged(plan: TagPlan, tags) -> bool:
    """True when every field this plan would write (other than ALBUM) is
    already non-blank on the file.

    ``tags`` can be any mutagen-style dict (FLAC or EasyID3). ALBUM is
    intentionally excluded — we always want to rewrite it to our
    canonical ``YYYY-MM-DD Venue, City [source]`` format.
    """
    if not _tag_present(tags, "ARTIST") and not _tag_present(tags, "artist"):
        return False
    if plan.date and not (_tag_present(tags, "DATE") or _tag_present(tags, "date")):
        return False
    if plan.track is not None and not (
        _tag_present(tags, "TRACKNUMBER") or _tag_present(tags, "tracknumber")
    ):
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


def _write_tags(plan: TagPlan) -> tuple[bool, bool]:
    """Apply *plan* to the destination file.

    Returns ``(changed, album_only)``:
    * ``changed`` — whether the file was actually saved.
    * ``album_only`` — True when every plan field except ALBUM was already
      populated, so we only rewrote ALBUM to match the canonical format.

    When the file is already fully tagged AND its ALBUM already equals
    our planned album, the file is left untouched.
    """
    ext = plan.dest.suffix.lower()
    if ext == ".flac":
        audio = FLAC(str(plan.dest))
        if _is_already_tagged(plan, audio):
            if _existing_album(audio) == plan.album:
                return False, True
            audio["ALBUM"] = plan.album
            audio.save()
            return True, True
        audio["ARTIST"] = plan.artist
        audio["ALBUMARTIST"] = plan.artist
        audio["ALBUM"] = plan.album
        if plan.date:
            audio["DATE"] = plan.date
        if plan.track is not None:
            audio["TRACKNUMBER"] = f"{plan.track:02d}"
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
        return True, False
    if ext == ".mp3":
        try:
            audio = EasyID3(str(plan.dest))
        except Exception:
            mp3 = MP3(str(plan.dest))
            mp3.add_tags()
            mp3.save()
            audio = EasyID3(str(plan.dest))
        if _is_already_tagged(plan, audio):
            if _existing_album(audio) == plan.album:
                return False, True
            audio["album"] = plan.album
            audio.save()
            return True, True
        audio["artist"] = plan.artist
        audio["albumartist"] = plan.artist
        audio["album"] = plan.album
        if plan.date:
            audio["date"] = plan.date
        if plan.track is not None:
            audio["tracknumber"] = f"{plan.track:02d}"
        if plan.title is not None:
            audio["title"] = plan.title
        if plan.disc is not None and plan.disc_total is not None:
            audio["discnumber"] = f"{plan.disc}/{plan.disc_total}"
        audio.save()
        return True, False
    raise RuntimeError(f"unsupported audio format: {ext}")
