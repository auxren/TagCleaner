"""Unit tests for tagcleaner.drafts — roundtrip, unicode, file I/O."""
from __future__ import annotations

import json
from pathlib import Path

from tagcleaner.drafts import (
    concert_from_dict,
    concert_to_dict,
    load_drafts,
    save_drafts,
)
from tagcleaner.models import Concert, SourceInfo, Track


def _sample(folder: Path) -> Concert:
    return Concert(
        folder=folder,
        artist="Rush",
        date="1984-09-21",
        venue="Maple Leaf Gardens",
        city="Toronto",
        region="Canada",
        source=SourceInfo(kind="SBD", mics=["Schoeps MK4"], taper="fear"),
        tracks=[
            Track(number=1, title="The Spirit of Radio"),
            Track(number=2, title="The Enemy Within"),
        ],
        audio_files=[folder / "01.flac", folder / "02.flac"],
        issues=["example"],
    )


class TestRoundtrip:
    def test_dict_roundtrip(self, tmp_path: Path):
        c = _sample(tmp_path)
        d = concert_to_dict(c)
        back = concert_from_dict(d)
        assert back.artist == c.artist
        assert back.date == c.date
        assert back.venue == c.venue
        assert back.city == c.city
        assert back.region == c.region
        assert back.source.kind == "SBD"
        assert back.source.mics == ["Schoeps MK4"]
        assert [t.title for t in back.tracks] == [t.title for t in c.tracks]
        assert [p.name for p in back.audio_files] == ["01.flac", "02.flac"]

    def test_file_roundtrip(self, tmp_path: Path):
        c = _sample(tmp_path)
        out = tmp_path / "drafts.json"
        save_drafts([c, c], out)
        loaded = load_drafts(out)
        assert len(loaded) == 2
        assert loaded[0].artist == "Rush"

    def test_album_and_confidence_written(self, tmp_path: Path):
        c = _sample(tmp_path)
        d = concert_to_dict(c)
        assert d["album"] == c.album_name()
        assert d["confidence"] == c.confidence()


class TestUnicodeSafety:
    def test_surrogate_bytes_in_filename_do_not_crash(self, tmp_path: Path):
        c = Concert(
            folder=tmp_path / "show",
            artist="Test",
            date="2000-01-01",
            audio_files=[tmp_path / "show" / "bad\udc8a.flac"],
            tracks=[Track(number=1, title="Glitch\udc8atitle")],
        )
        d = concert_to_dict(c)
        # JSON must be serializable + writable as UTF-8.
        path = tmp_path / "d.json"
        save_drafts([c], path)  # must not raise
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert len(raw) == 1
        # Lone surrogates should have been replaced with U+FFFD.
        assert "\udc8a" not in raw[0]["tracks"][0]["title"]

    def test_none_fields_stay_none(self, tmp_path: Path):
        c = Concert(folder=tmp_path / "show")
        d = concert_to_dict(c)
        assert d["artist"] is None
        assert d["date"] is None
