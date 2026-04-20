"""Unit tests for tagcleaner.parser — dates, artists, setlists, integration."""
from __future__ import annotations

from pathlib import Path

import pytest

from tagcleaner.parser import (
    _city_from_folder,
    _finalize_tracks,
    _split_venue_city_region,
    build_concert,
    guess_artist_from_folder,
    parse_date,
    parse_info_txt,
    parse_setlist,
    read_info_txt,
)


class TestParseDate:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("1984-09-21", "1984-09-21"),
            ("1984/09/21", "1984-09-21"),
            ("1984.09.21", "1984-09-21"),
            ("los1996-03-20", "1996-03-20"),
            ("rush1984-09-21.sbd", "1984-09-21"),
            ("19840921", "1984-09-21"),
            ("SRV_1985.0725_Ottawa", "1985-07-25"),
            ("sbd_1985_0725", "1985-07-25"),
            ("August 22, 1987", "1987-08-22"),
            ("22nd Aug 1987", "1987-08-22"),
            ("22 August 1987", "1987-08-22"),
            ("84-09-21", "1984-09-21"),
        ],
    )
    def test_recognized_formats(self, text: str, expected: str):
        assert parse_date(text) == expected

    @pytest.mark.parametrize("text", ["no date here", "1984-99-99", "2025-13-01", ""])
    def test_invalid_dates_return_none(self, text: str):
        assert parse_date(text) is None


class TestGuessArtist:
    @pytest.mark.parametrize(
        "folder,expected",
        [
            ("gd67-08-05.sbd", "Grateful Dead"),
            ("rush1984-09-21.sbd.fear.flac16", "Rush"),
            ("los1996-03-20", "Los Lobos"),
            ("ph1997-12-31", "Phish"),
            ("srv1985-07-25", "Stevie Ray Vaughan"),
            ("Talking Heads 1980-08-27 Wollman Rink", "Talking Heads"),
            ("Grateful Dead - 1987-08-22 - Calaveras", "Grateful Dead"),
        ],
    )
    def test_known_patterns(self, folder: str, expected: str):
        assert guess_artist_from_folder(folder) == expected

    def test_unknown_prefix_returns_none(self):
        assert guess_artist_from_folder("xyz_unparseable") is None


class TestSetlistParser:
    def test_single_disc_flattens(self):
        body = "01. Psycho Killer\n02. Warning Signs\n03. Stay Hungry\n"
        tracks = _finalize_tracks(parse_setlist(body))
        assert [t.title for t in tracks] == ["Psycho Killer", "Warning Signs", "Stay Hungry"]
        assert [t.number for t in tracks] == [1, 2, 3]
        assert all(t.disc is None for t in tracks)

    def test_set_markers_split_into_discs(self):
        body = (
            "Set 1:\n"
            "01. Touch of Grey\n"
            "02. Minglewood Blues\n"
            "Set 2:\n"
            "01. Scarlet Begonias\n"
            "Encore:\n"
            "01. U.S. Blues\n"
        )
        tracks = _finalize_tracks(parse_setlist(body))
        assert len(tracks) == 4
        assert tracks[0].disc == 1 and tracks[0].number == 1
        assert tracks[1].disc == 1 and tracks[1].number == 2
        assert tracks[2].disc == 2 and tracks[2].number == 1
        assert tracks[3].disc == 3 and tracks[3].number == 1
        assert all(t.disc_total == 3 for t in tracks)

    def test_lone_disc_one_marker_collapses_to_single_disc(self):
        body = "Disc One\n01. Song A\n02. Song B\n"
        tracks = _finalize_tracks(parse_setlist(body))
        assert len(tracks) == 2
        assert all(t.disc is None for t in tracks)

    def test_cd_marker_variations(self):
        body = "CD 1:\n01. A\nCD 2:\n01. B\n"
        tracks = _finalize_tracks(parse_setlist(body))
        discs = {t.disc for t in tracks}
        assert discs == {1, 2}

    def test_early_show_is_disc_one(self):
        body = "Early Show\n01. A\nLate Show\n01. B\n"
        tracks = _finalize_tracks(parse_setlist(body))
        assert tracks[0].disc == 1
        assert tracks[1].disc == 2

    def test_checksum_lines_are_skipped(self):
        body = (
            "01. Real Song\n"
            "01. md5 deadbeefdeadbeefdeadbeefdeadbeef\n"
            "02. Another Song\n"
        )
        tracks = _finalize_tracks(parse_setlist(body))
        assert [t.title for t in tracks] == ["Real Song", "Another Song"]

    def test_dot_dash_paren_separators(self):
        body = "01. A\n02 - B\n03) C\n04 D\n"
        tracks = _finalize_tracks(parse_setlist(body))
        assert [t.title for t in tracks] == ["A", "B", "C", "D"]


class TestParseInfoTxt:
    def test_artist_on_first_line(self):
        body = "Grateful Dead\n1987-08-22\nCalaveras County Fairgrounds\nAngels Camp, CA\n"
        out = parse_info_txt(body)
        assert out["artist"] == "Grateful Dead"
        assert out["date"] == "1987-08-22"

    def test_labeled_fields_win(self):
        body = "random header\nArtist: Phish\nVenue: Madison Square Garden\nCity: New York, NY\n"
        out = parse_info_txt(body)
        assert out["artist"] == "Phish"
        assert out["venue"] == "Madison Square Garden"
        assert out["city"] == "New York, NY"

    def test_noise_first_line_is_skipped(self):
        body = "No errors occured.\nTalking Heads\n1980-08-27\n"
        out = parse_info_txt(body)
        assert out["artist"] == "Talking Heads"

    def test_venue_first_line_not_classified_as_artist(self):
        body = (
            "Henry J. Kaiser Convention Center, Oakland, CA\n"
            "1984-04-14\n"
            "Grateful Dead\n"
            "01. Alabama Getaway\n"
        )
        out = parse_info_txt(body)
        assert out["artist"] == "Grateful Dead"
        assert out["venue"] == "Henry J. Kaiser Convention Center"
        assert out["city"] == "Oakland"
        assert out["region"] == "CA"

    def test_composite_venue_city_region_split(self):
        body = (
            "Phish\n"
            "1997-12-31\n"
            "Madison Square Garden, New York, NY\n"
            "01. NICU\n"
        )
        out = parse_info_txt(body)
        assert out["venue"] == "Madison Square Garden"
        assert out["city"] == "New York"
        assert out["region"] == "NY"

    def test_composite_without_known_region_not_split(self):
        # Three commas but last part isn't a state/country — don't invent.
        body = "Some Band\n1999-01-01\nRandom, Thing, Other\n01. a\n"
        out = parse_info_txt(body)
        assert out.get("venue") != "Random"

    def test_city_line_alone_not_classified_as_artist(self):
        body = "Oakland, CA\n1984-04-14\nGrateful Dead\n01. Alabama Getaway\n"
        out = parse_info_txt(body)
        assert out["artist"] == "Grateful Dead"
        assert out.get("city") == "Oakland"


class TestReadInfoTxt:
    def test_utf8_plain(self, tmp_path: Path):
        p = tmp_path / "info.txt"
        p.write_bytes("Grateful Dead\n".encode("utf-8"))
        assert read_info_txt(p).startswith("Grateful Dead")

    def test_utf8_bom(self, tmp_path: Path):
        p = tmp_path / "info.txt"
        p.write_bytes(b"\xef\xbb\xbf" + "Grateful Dead\n".encode("utf-8"))
        text = read_info_txt(p)
        assert text.startswith("Grateful Dead")
        assert not text.startswith("\ufeff")

    def test_utf16_le_bom(self, tmp_path: Path):
        p = tmp_path / "info.txt"
        p.write_bytes("Grateful Dead\n1987-08-22\n".encode("utf-16-le"))
        # Prepend BOM
        p.write_bytes(b"\xff\xfe" + "Grateful Dead\n1987-08-22\n".encode("utf-16-le"))
        assert "Grateful Dead" in read_info_txt(p)

    def test_utf16_be_bom(self, tmp_path: Path):
        p = tmp_path / "info.txt"
        p.write_bytes(b"\xfe\xff" + "Grateful Dead\n1987-08-22\n".encode("utf-16-be"))
        assert "Grateful Dead" in read_info_txt(p)

    def test_utf16_le_no_bom_detected(self, tmp_path: Path):
        # Older Notepad saves UTF-16-LE without a BOM; the zero-byte density
        # heuristic should catch it.
        p = tmp_path / "info.txt"
        body = ("Grateful Dead\n" * 20).encode("utf-16-le")
        p.write_bytes(body)
        assert "Grateful Dead" in read_info_txt(p)

    def test_empty_file(self, tmp_path: Path):
        p = tmp_path / "info.txt"
        p.write_bytes(b"")
        assert read_info_txt(p) == ""

    def test_missing_file(self, tmp_path: Path):
        assert read_info_txt(tmp_path / "nope.txt") == ""


class TestSplitVenueCityRegion:
    def test_state_code(self):
        assert _split_venue_city_region("Wollman Rink, New York, NY") == (
            "Wollman Rink", "New York", "NY",
        )

    def test_country(self):
        assert _split_venue_city_region("Wembley Stadium, London, England") == (
            "Wembley Stadium", "London", "England",
        )

    def test_unknown_tail_rejected(self):
        assert _split_venue_city_region("A, B, C") == (None, None, None)

    def test_two_parts_rejected(self):
        assert _split_venue_city_region("City, NY") == (None, None, None)

    def test_extra_commas_in_venue(self):
        v, c, r = _split_venue_city_region("The Venue, Second Stage, Austin, TX")
        assert v == "The Venue, Second Stage"
        assert c == "Austin"
        assert r == "TX"


class TestCityFromFolder:
    @pytest.mark.parametrize(
        "folder,city,region",
        [
            ("SomeArtist 1987-08-22 Big Venue, Angels Camp, CA", "Angels Camp", "CA"),
            ("Rush 1984-09-21 Maple Leaf Gardens, Toronto, Canada", "Toronto", "Canada"),
        ],
    )
    def test_extracts_city_state(self, folder: str, city: str, region: str):
        c, r = _city_from_folder(folder)
        assert c == city
        assert r == region


class TestBuildConcertIntegration:
    def test_from_info_txt(self, tmp_path: Path, make_flac):
        folder = tmp_path / "gd1987-08-22"
        folder.mkdir()
        audio = [
            make_flac(folder / "01 Touch of Grey.flac"),
            make_flac(folder / "02 Hell in a Bucket.flac"),
        ]
        info = folder / "info.txt"
        info.write_text(
            "Grateful Dead\n"
            "1987-08-22\n"
            "Calaveras County Fairgrounds\n"
            "Angels Camp, CA\n"
            "Soundboard\n"
            "\n"
            "01. Touch of Grey\n"
            "02. Hell in a Bucket\n",
            encoding="utf-8",
        )
        c = build_concert(folder, audio, info)
        assert c.artist == "Grateful Dead"
        assert c.date == "1987-08-22"
        assert c.venue == "Calaveras County Fairgrounds"
        assert c.city == "Angels Camp"
        assert c.region == "CA"
        assert c.source.kind == "SBD"
        assert len(c.tracks) == 2
        assert [t.title for t in c.tracks] == ["Touch of Grey", "Hell in a Bucket"]
        assert not c.issues
        assert c.confidence() == 1.0
        assert c.album_name() == (
            "1987-08-22 Calaveras County Fairgrounds, Angels Camp, CA [SBD]"
        )

    def test_folder_name_beats_info_txt_date(self, tmp_path: Path, make_flac):
        folder = tmp_path / "rush1984-09-21.sbd"
        folder.mkdir()
        audio = [make_flac(folder / "01 A.flac")]
        info = folder / "notes.txt"
        # info.txt mentions a remaster date — folder date must still win.
        info.write_text("Rush\nRemastered 2020-05-15\n01. A\n", encoding="utf-8")
        c = build_concert(folder, audio, info)
        assert c.date == "1984-09-21"

    def test_track_mismatch_flagged(self, tmp_path: Path, make_flac):
        folder = tmp_path / "ph2000-01-01"
        folder.mkdir()
        audio = [make_flac(folder / f"0{i} t.flac") for i in (1, 2, 3)]
        info = folder / "info.txt"
        info.write_text("Phish\n2000-01-01\n01. A\n02. B\n", encoding="utf-8")
        c = build_concert(folder, audio, info)
        assert any("track count mismatch" in issue for issue in c.issues)

    def test_no_setlist_issue(self, tmp_path: Path, make_flac):
        folder = tmp_path / "unknown 1999-05-05"
        folder.mkdir()
        audio = [make_flac(folder / "01 something.flac")]
        c = build_concert(folder, audio, None)
        assert "no setlist found" in c.issues
