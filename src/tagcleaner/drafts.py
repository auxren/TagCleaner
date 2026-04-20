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
    # Linux filesystems can hand us filenames with non-UTF-8 bytes; Python
    # surfaces those as lone UTF-16 surrogates via surrogateescape. JSON
    # can serialize them, but write_text(encoding="utf-8") can't, so scrub
    # them to U+FFFD before they reach the serializer.
    return {
        "folder": _clean(str(c.folder)),
        "artist": _clean(c.artist),
        "date": _clean(c.date),
        "venue": _clean(c.venue),
        "city": _clean(c.city),
        "region": _clean(c.region),
        "source": _clean_source(asdict(c.source)),
        "album": _clean(c.album_name()),
        "confidence": c.confidence(),
        "issues": [_clean(i) for i in c.issues],
        "audio_files": [_clean(str(p)) for p in c.audio_files],
        "tracks": [
            {
                "number": t.number,
                "title": _clean(t.title),
                "disc": t.disc,
                "disc_total": t.disc_total,
            }
            for t in c.tracks
        ],
    }


def _clean(s):
    if s is None:
        return None
    if isinstance(s, str):
        return s.encode("utf-8", "replace").decode("utf-8")
    return s


def _clean_source(d: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": _clean(d.get("kind")),
        "mics": [_clean(m) for m in (d.get("mics") or [])],
        "taper": _clean(d.get("taper")),
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
    data = concerts_to_json(concerts).encode("utf-8", "replace")
    path.write_bytes(data)


def load_drafts(path: Path) -> list[Concert]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return [concert_from_dict(d) for d in data]
