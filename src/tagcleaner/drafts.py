"""Serialize/deserialize scanner output so users can review and edit drafts before writing tags.

The per-concert dict shape produced by ``concert_to_dict`` is reused by
the history file, so anything that reads drafts JSON also reads history
entries without translation.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .models import Concert, SourceInfo, Track


def concert_to_dict(c: Concert) -> dict[str, Any]:
    return {
        "folder": str(c.folder),
        "artist": c.artist,
        "date": c.date,
        "venue": c.venue,
        "city": c.city,
        "region": c.region,
        "source": asdict(c.source),
        "album": c.album_name(),
        "confidence": c.confidence(),
        "issues": list(c.issues),
        "audio_files": [str(p) for p in c.audio_files],
        "tracks": [
            {"number": t.number, "title": t.title, "disc": t.disc, "disc_total": t.disc_total}
            for t in c.tracks
        ],
    }


def concert_from_dict(d: dict[str, Any]) -> Concert:
    return Concert(
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
    )


def concerts_to_json(concerts: list[Concert]) -> str:
    return json.dumps([concert_to_dict(c) for c in concerts], indent=2, ensure_ascii=False)


def save_drafts(concerts: list[Concert], path: Path) -> None:
    path.write_text(concerts_to_json(concerts), encoding="utf-8")


def load_drafts(path: Path) -> list[Concert]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return [concert_from_dict(d) for d in data]
