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
    track: int
    title: str
    disc: int | None = None
    disc_total: int | None = None


@dataclass
class TagResult:
    plan: TagPlan
    ok: bool
    error: str | None = None


def build_plans(concert: Concert, *, copy_to_root: Path | None = None, source_root: Path | None = None) -> list[TagPlan]:
    """Pair parsed tracks with audio files and produce a TagPlan per file.
    If track/audio counts mismatch we pair by position up to the shorter list;
    the caller is expected to have surfaced the issue already."""
    if not concert.audio_files or not concert.tracks:
        return []
    pairs = list(zip(concert.audio_files, concert.tracks))
    album = concert.album_name()
    artist = concert.artist or "Unknown Artist"
    date = concert.date or ""
    plans: list[TagPlan] = []
    for audio, track in pairs:
        dest = audio
        if copy_to_root is not None and source_root is not None:
            rel = audio.relative_to(source_root)
            dest = copy_to_root / rel
        plans.append(TagPlan(
            file=audio,
            dest=dest,
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
            _write_tags(plan)
            results.append(TagResult(plan=plan, ok=True))
        except Exception as exc:  # noqa: BLE001 - we want to report every failure
            results.append(TagResult(plan=plan, ok=False, error=f"{type(exc).__name__}: {exc}"))
    return results


def _write_tags(plan: TagPlan) -> None:
    ext = plan.dest.suffix.lower()
    if ext == ".flac":
        audio = FLAC(str(plan.dest))
        audio["ARTIST"] = plan.artist
        audio["ALBUMARTIST"] = plan.artist
        audio["ALBUM"] = plan.album
        if plan.date:
            audio["DATE"] = plan.date
        audio["TRACKNUMBER"] = f"{plan.track:02d}"
        audio["TITLE"] = plan.title
        if plan.disc is not None and plan.disc_total is not None:
            audio["DISCNUMBER"] = str(plan.disc)
            audio["DISCTOTAL"] = str(plan.disc_total)
        else:
            for k in ("DISCNUMBER", "DISCTOTAL"):
                if k in audio:
                    del audio[k]
        audio.save()
        return
    if ext == ".mp3":
        try:
            audio = EasyID3(str(plan.dest))
        except Exception:
            mp3 = MP3(str(plan.dest))
            mp3.add_tags()
            mp3.save()
            audio = EasyID3(str(plan.dest))
        audio["artist"] = plan.artist
        audio["albumartist"] = plan.artist
        audio["album"] = plan.album
        if plan.date:
            audio["date"] = plan.date
        audio["tracknumber"] = f"{plan.track:02d}"
        audio["title"] = plan.title
        if plan.disc is not None and plan.disc_total is not None:
            audio["discnumber"] = f"{plan.disc}/{plan.disc_total}"
        audio.save()
        return
    raise RuntimeError(f"unsupported audio format: {ext}")
