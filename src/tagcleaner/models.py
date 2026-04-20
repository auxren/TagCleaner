"""Data classes shared across the package."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class Track:
    number: int           # track number within its disc (1-based)
    title: str
    disc: Optional[int] = None
    disc_total: Optional[int] = None


@dataclass
class SourceInfo:
    """Recording-source metadata used to disambiguate multiple transfers of
    the same show (e.g. 'SBD', 'AUD Schoeps MK4', 'FM')."""

    kind: Optional[str] = None       # SBD | AUD | FM | Pre-FM | Matrix | MTX | Mixed | None
    mics: list[str] = field(default_factory=list)  # e.g. ['Schoeps MK4', 'AKG 200E']
    taper: Optional[str] = None      # free-form taper/remaster credit

    def label(self) -> str:
        """Short bracketed label for the album name, or '' if unknown."""
        parts: list[str] = []
        if self.kind:
            parts.append(self.kind)
        if self.mics:
            parts.extend(self.mics)
        if not parts:
            return ""
        return "[" + " ".join(parts) + "]"


@dataclass
class Concert:
    folder: Path
    artist: Optional[str] = None
    date: Optional[str] = None       # ISO YYYY-MM-DD
    venue: Optional[str] = None
    city: Optional[str] = None
    region: Optional[str] = None     # state / country
    source: SourceInfo = field(default_factory=SourceInfo)
    tracks: list[Track] = field(default_factory=list)
    audio_files: list[Path] = field(default_factory=list)
    info_txt: Optional[Path] = None
    issues: list[str] = field(default_factory=list)

    def album_name(self) -> str:
        """Build the canonical album string: `YYYY-MM-DD Venue, City, Region [source]`."""
        parts: list[str] = []
        if self.date:
            parts.append(self.date)
        place: list[str] = []
        if self.venue:
            place.append(self.venue)
        if self.city:
            place.append(self.city)
        if self.region:
            place.append(self.region)
        if place:
            parts.append(", ".join(place))
        album = " ".join(parts).strip()
        src = self.source.label()
        if src:
            album = f"{album} {src}".strip()
        return album

    def confidence(self) -> float:
        """Rough 0.0-1.0 score. 1.0 = all fields present and track count matches audio count."""
        score = 0.0
        if self.artist: score += 0.25
        if self.date: score += 0.25
        if self.venue: score += 0.15
        if self.city: score += 0.10
        if self.tracks and self.audio_files:
            if len(self.tracks) == len(self.audio_files):
                score += 0.25
            else:
                score += 0.05
        return round(score, 2)
