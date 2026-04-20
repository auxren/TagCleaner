"""Serialize/deserialize scanner output so users can review and edit drafts before writing tags."""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from .models import Concert, SourceInfo, Track


def concerts_to_json(concerts: list[Concert]) -> str:
    items = []
    for c in concerts:
        items.append({
            "folder": str(c.folder),
            "artist": c.artist,
            "date": c.date,
            "venue": c.venue,
            "city": c.city,
            "region": c.region,
            "source": asdict(c.source),
            "album": c.album_name(),
            "confidence": c.confidence(),
            "issues": c.issues,
            "audio_files": [str(p) for p in c.audio_files],
            "tracks": [
                {"number": t.number, "title": t.title, "disc": t.disc, "disc_total": t.disc_total}
                for t in c.tracks
            ],
        })
    return json.dumps(items, indent=2, ensure_ascii=False)


def save_drafts(concerts: list[Concert], path: Path) -> None:
    path.write_text(concerts_to_json(concerts), encoding="utf-8")


def load_drafts(path: Path) -> list[Concert]:
    data = json.loads(path.read_text(encoding="utf-8"))
    concerts: list[Concert] = []
    for d in data:
        concerts.append(Concert(
            folder=Path(d["folder"]),
            artist=d.get("artist"),
            date=d.get("date"),
            venue=d.get("venue"),
            city=d.get("city"),
            region=d.get("region"),
            source=SourceInfo(**(d.get("source") or {})),
            tracks=[Track(**t) for t in d.get("tracks", [])],
            audio_files=[Path(p) for p in d.get("audio_files", [])],
            info_txt=None,
            issues=list(d.get("issues", [])),
        ))
    return concerts
