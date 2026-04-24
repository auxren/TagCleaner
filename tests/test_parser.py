"""Unit tests for tagcleaner.parser — dates, artists, setlists, integration."""
from __future__ import annotations

from pathlib import Path

import pytest

from tagcleaner.lexicon import Lexicon
from tagcleaner.parser import (
    _city_from_folder,
    _finalize_tracks,
    _lexicon_artist_from_parent,
    _split_venue_city_region,
    _trust_parent_artist,
    build_concert,
    guess_artist_from_folder,
    parse_date,
    parse_info_txt,
    parse_setlist,
    read_info_txt,
    weak_artist_from_folder,
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
            # US-style MM/DD/YY with 2-digit year (year<60 → 20xx, else 19xx).
            ("11_10_23 Chicago", "2023-11-10"),
            ("09_01_89 Merriweather", "1989-09-01"),
            ("03-28-13", "2013-03-28"),
            # US-style MM/DD/YYYY with full year.
            ("02/19/2010 - Friday", "2010-02-19"),
            ("11.10.2023", "2023-11-10"),
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
            # Folder uses underscore-separated 2-digit-year dates — the
            # single most common case we'd been missing entirely.
            ("My Morning Jacket - 11_10_23 Live from The Chicago Theatre, Chicago, IL",
             "My Morning Jacket"),
            ("Jerry Garcia Band - 09_01_89 Merriweather Post Pavilion, Columbia, MD",
             "Jerry Garcia Band"),
            ("Widespread Panic - 06_25_16 Red Rocks", "Widespread Panic"),
            # No space before the dash (seen with CRB etc.).
            ("CRB- 2012-08-24 Bearsville Theatre, Woodstock, NY", "CRB"),
            # Year-only boundary: the artist is whatever's before ' - '.
            ("Steel Pulse - The Palace,Hollywood, CA 1985 SBD quest Stevie Wonder (Upgrade)",
             "Steel Pulse"),
            # Prose-date boundary: take the prefix up to the first ' - '.
            ("Steel Pulse - Sunsplash - JA Aug. 21st 1987  SBD4", "Steel Pulse"),
        ],
    )
    def test_known_patterns(self, folder: str, expected: str):
        assert guess_artist_from_folder(folder) == expected

    def test_unknown_prefix_returns_none(self):
        assert guess_artist_from_folder("xyz_unparseable") is None

    @pytest.mark.parametrize("folder", [
        # No alphabetic content before the date.
        "2007 10 12 I Camden NJ",
        # Pure date folder.
        "1984-09-21",
    ])
    def test_dateless_prefixes_return_none(self, folder: str):
        assert guess_artist_from_folder(folder) is None


class TestWeakArtistFromFolder:
    """Last-resort folder-name extraction. Only consulted in
    ``build_concert`` after the lexicon walk and ``_trust_parent_artist``
    have already come up empty — so returning the wrong answer is
    acceptable, but returning a venue/prose string masquerading as an
    artist is not (it pollutes the lexicon)."""

    @pytest.mark.parametrize("folder,expected", [
        # Artist living AFTER a leading year. ~47 folders in the live
        # library have this shape.
        ("1969 Black Sabbath", "Black Sabbath"),
        ("1971 Nice FM", "Nice FM"),
        ("20150515 U2 Vancouver", "U2 Vancouver"),
        ("20120101 The Strokes", "The Strokes"),
    ])
    def test_artist_after_leading_date(self, folder: str, expected: str):
        assert weak_artist_from_folder(folder) == expected

    @pytest.mark.parametrize("folder,expected", [
        # Folder name IS the artist — ~80 folders in the live library.
        ("All Them Witches", "All Them Witches"),
        ("Aphex Twin", "Aphex Twin"),
        ("Benny Goodman", "Benny Goodman"),
        ("Howard Jones", "Howard Jones"),
        ("Billy Talent", "Billy Talent"),
    ])
    def test_bare_folder_name_as_artist(self, folder: str, expected: str):
        assert weak_artist_from_folder(folder) == expected

    @pytest.mark.parametrize("folder", [
        # Venue/city names — don't treat as artist.
        "Aragon Ballroom - Chicago",
        "Boston MA",
        "The Supper Club New York NY",
        # Prose / description lines.
        "music",
        "Master of Reality FLAC 24bit",
        # Shouldn't override stronger signals — "2007 10 12 I Camden NJ"
        # has too much noise after the date to salvage an artist from.
        "2007 10 12 I Camden NJ",
    ])
    def test_venue_or_prose_rejected(self, folder: str):
        got = weak_artist_from_folder(folder)
        assert got != folder, f"{folder!r} should not be returned verbatim"


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

    def test_colon_separator_etree_format(self):
        """Some old etree info.txt files use `N: title  M:SS` format
        (number-colon-space-title-spaces-duration). Parser should accept
        the colon and strip the trailing duration."""
        body = (
            "  1: tuning  1:09\n"
            "  2: the wizard  8:46\n"
            "  3: midnight tango  8:07\n"
            "  4: race with the devil on the Spanish highway  10:52\n"
        )
        tracks = _finalize_tracks(parse_setlist(body))
        assert [t.title for t in tracks] == [
            "tuning", "the wizard", "midnight tango",
            "race with the devil on the Spanish highway",
        ]
        assert [t.number for t in tracks] == [1, 2, 3, 4]

    def test_colon_separator_requires_following_space(self):
        """`12:34 Some Text` should NOT parse as track 12 with title
        '34 Some Text' — the colon-as-separator only fires when followed
        by a real space, distinguishing it from durations/timestamps."""
        body = "12:34 elapsed time\n01. Real Track\n"
        tracks = _finalize_tracks(parse_setlist(body))
        # Only the real track should parse — the timestamp line has no space
        # after the colon's digit (12:34 has digit-colon-digit).
        assert [t.title for t in tracks] == ["Real Track"]

    def test_underscore_separator(self):
        """Some etree info files use `01_ Title` (number-underscore-space-
        title). Seen on David Bowie / Stones bootleg trees."""
        body = "01_ Intro\n02_ The Ties That Bind\n03_ Born to Run\n"
        tracks = _finalize_tracks(parse_setlist(body))
        assert [t.title for t in tracks] == ["Intro", "The Ties That Bind", "Born to Run"]

    def test_trailing_duration_stripped(self):
        """Bare `:47` durations (intros etc.) and `M:SS` durations should
        be stripped from titles regardless of separator style."""
        body = (
            "01. Intro  :47\n"
            "02. Main Event  12:34\n"
            "03 - Encore  3:00\n"
        )
        tracks = _finalize_tracks(parse_setlist(body))
        assert [t.title for t in tracks] == ["Intro", "Main Event", "Encore"]


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

    def test_descriptive_sentence_rejected_as_artist(self):
        # Real-world header from a Steel Pulse folder. The banner line is
        # rejected (Palace is a venue keyword), the next line looks clean
        # enough to a naive filter but is clearly a description sentence.
        body = (
            "********** Steel Pulse - The Palace ************\n"
            "This is an incredible show with special guest Stevie Wonder.\n"
            "01. reggae fever\n"
        )
        out = parse_info_txt(body)
        assert out.get("artist") != \
            "This is an incredible show with special guest Stevie Wonder."

    def test_line_with_embedded_date_rejected_as_artist(self):
        # "Artist - Date - Source" slug from a taper's first line. The date
        # (8-21-87) and the source code (SBD4) each independently disqualify
        # it from being used as an artist.
        body = (
            "Steel Pulse - Sunsplash - JA 8-21-87 SBD4\n"
            "01. Steel Pulse - Sunsplash - t. cownan intro\n"
        )
        out = parse_info_txt(body)
        assert "Steel Pulse - Sunsplash - JA 8-21-87 SBD4" != out.get("artist")

    def test_track_line_not_promoted_to_venue(self):
        # Info.txt that's essentially just the setlist, no venue present:
        # the first track line used to be picked up as the venue.
        body = (
            "Some Band\n"
            "1999-06-15\n"
            "01. Opener Song\n"
            "02. Middle Tune\n"
            "03. Closer\n"
        )
        out = parse_info_txt(body)
        assert not out.get("venue", "").startswith("01.")


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

    def test_rtf_stripped_to_plain(self, tmp_path: Path):
        # Mac TextEdit's default RTF preamble — parser used to report this
        # whole line as the artist. Real RTF always has a delimiter space (or
        # non-letter) between a control word and following text.
        p = tmp_path / "info.txt"
        rtf = (
            "{\\rtf1\\ansi\\ansicpg1252\\cocoartf1138\\cocoasubrtf510\n"
            "{\\fonttbl\\f0\\fswiss\\fcharset0 Helvetica;}\n"
            "{\\colortbl;\\red255\\green255\\blue255;}\n"
            "\\paperw12240\\paperh15840\\margl1440\\margr1440\n"
            "\\pard\\pardirnatural\n"
            "\\f0\\fs24 \\cf0 Smashing Pumpkins\\par\n"
            " 2010-12-05\\par\n"
            " Verizon Wireless Theater, Houston, TX}"
        )
        p.write_bytes(rtf.encode("utf-8"))
        text = read_info_txt(p)
        assert "\\rtf" not in text
        assert "cocoartf" not in text
        assert "fonttbl" not in text
        assert "Smashing Pumpkins" in text
        assert "2010-12-05" in text
        assert "Verizon Wireless Theater" in text


class TestMicPlacementRejected:
    """Mic-placement jargon (FOB, DFC, DIN, ORTF, 'mics @ 10ft') is recording
    geometry, not venue or artist. These lines show up in real info.txt bodies
    and used to get promoted to the artist or venue slot."""

    @pytest.mark.parametrize("line", [
        "Right of Center, FOB / Mics @ 10 ft. DIN",
        "DFC / 6' from stage / DIN / mic stand @ 6'",
        "Balcony, right stack, mics clamped to the rail",
        "Main Stage, at the SBD, ROC",
        "FOB, 10ft high, ORTF pair",
    ])
    def test_not_picked_as_artist(self, line: str):
        # Body with placement line before a real artist line -- artist should
        # be the real one, not the placement line.
        body = f"{line}\nPhish\n1997-12-31\nMadison Square Garden, New York, NY\n"
        data = parse_info_txt(body)
        assert data.get("artist") == "Phish"

    @pytest.mark.parametrize("line", [
        "FOB / Mics @ 10 ft. DIN",
        "DFC / 6' from stage / DIN",
        "Balcony, right stack, mics clamped to the rail",
    ])
    def test_not_picked_as_venue(self, line: str):
        body = f"Phish\n1997-12-31\n{line}\n"
        data = parse_info_txt(body)
        assert data.get("venue") != line
        # And not a substring either — should fall through, not set venue.
        if "venue" in data:
            assert "FOB" not in data["venue"] and "DFC" not in data["venue"]


class TestVenueNoiseRejected:
    """Taper notes often slip lineage chains, section headers, or
    parenthesised source-kind prefixes into the venue slot. These test bodies
    are verbatim shapes seen in real-world info.txt files."""

    def test_lineage_chain_rejected_as_venue(self):
        # Lineage chains with '>'-arrows sometimes have a city tail that makes
        # _split_venue_city_region take the bait. Reject the whole line.
        body = (
            "Grateful Dead\n"
            "1987-08-22\n"
            "dsp-quattro 3 > MBIT+ > 16/44.1 wav > xACT 2.12 > flac, Glenside, PA\n"
            "01. Tune\n"
        )
        data = parse_info_txt(body)
        assert ">" not in data.get("venue", "")
        assert "dsp-quattro" not in data.get("venue", "")

    def test_labeled_venue_with_lineage_rejected(self):
        body = (
            "Grateful Dead\n"
            "1987-08-22\n"
            "Venue: AKG > Sony PCM > flac\n"
        )
        data = parse_info_txt(body)
        assert "venue" not in data or "AKG" not in data["venue"]

    def test_transfer_info_not_picked_as_venue(self):
        body = (
            "Phish\n"
            "1997-12-31\n"
            "Transfer Info: JB3 -> SoundForge 6.0 -> CDWave -> flac\n"
            "01. NICU\n"
        )
        data = parse_info_txt(body)
        assert "venue" not in data or "Transfer" not in data["venue"]

    def test_the_recording_header_not_picked_as_venue(self):
        body = (
            "Phish\n"
            "1997-12-31\n"
            "The Recording: mics clamped to the rail at 10ft\n"
            "01. NICU\n"
        )
        data = parse_info_txt(body)
        assert "venue" not in data or "Recording" not in data["venue"]

    def test_parenthesised_source_kind_prefix_rejected(self):
        body = (
            "Phish\n"
            "1997-12-31\n"
            "(D-sbd), recorded from the board feed\n"
            "01. NICU\n"
        )
        data = parse_info_txt(body)
        assert "venue" not in data or "D-sbd" not in data["venue"]

    def test_mm_dd_yyyy_date_line_rejected_as_venue(self):
        # parse_date doesn't parse American-style MM/DD/YYYY; the dense-digit
        # guard in _looks_like_venue catches it instead.
        body = (
            "Phish\n"
            "1997-12-31\n"
            "02/19/2010 - Friday\n"
            "01. NICU\n"
        )
        data = parse_info_txt(body)
        assert "venue" not in data or "02/19/2010" not in data["venue"]

    def test_real_venue_still_accepted(self):
        body = (
            "Phish\n"
            "1997-12-31\n"
            "Madison Square Garden\n"
            "New York, NY\n"
            "01. NICU\n"
        )
        data = parse_info_txt(body)
        assert data["venue"] == "Madison Square Garden"
        assert data["city"] == "New York"
        assert data["region"] == "NY"

    def test_labeled_venue_still_accepted(self):
        body = (
            "Phish\n"
            "1997-12-31\n"
            "Venue: Madison Square Garden\n"
        )
        data = parse_info_txt(body)
        assert data["venue"] == "Madison Square Garden"


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

    def test_weak_folder_fallback_fires_when_no_other_signal(
        self, tmp_path: Path, make_flac
    ):
        # Bare artist folder with audio directly inside, no info.txt.
        # Wrap in "Tapes" so parent-trust stops at the library root and
        # weak_artist_from_folder is the real last line of defence.
        root = tmp_path / "Tapes"
        root.mkdir()
        folder = root / "Howard Jones"
        folder.mkdir()
        audio = [make_flac(folder / "01.flac"), make_flac(folder / "02.flac")]
        c = build_concert(folder, audio, None)
        assert c.artist == "Howard Jones"

    def test_weak_fallback_not_preferred_over_parent_trust(
        self, tmp_path: Path, make_flac
    ):
        # Parent is "Black Sabbath", leaf is "1969 Unknown Show" — the
        # leading-date tail would yield "Unknown Show" but parent-trust
        # should win with "Black Sabbath".
        root = tmp_path / "Tapes"
        root.mkdir()
        folder = root / "Black Sabbath" / "1969 Unknown Show"
        folder.mkdir(parents=True)
        audio = [make_flac(folder / "01.flac")]
        c = build_concert(folder, audio, None)
        assert c.artist == "Black Sabbath"

    def test_no_setlist_issue(self, tmp_path: Path, make_flac):
        folder = tmp_path / "unknown 1999-05-05"
        folder.mkdir()
        audio = [make_flac(folder / "01 something.flac")]
        c = build_concert(folder, audio, None)
        assert "no setlist found" in c.issues


class TestTrustParentArtist:
    """Last-resort artist fallback that walks the ancestor chain looking for
    a clean artist-shape folder name. Catches two real-world gaps:

    * brand-new ``Tapes/Artist/Show/`` libraries where the artist has zero
      prior history (so the lexicon walk can't confirm it);
    * deeply-nested ``Show/Show/Audio/CD1`` wrappers where the artist sits
      3+ levels above the concert folder.

    The function operates on ``Path`` objects only — it does not touch the
    filesystem, so tests can construct synthetic paths."""

    @pytest.mark.parametrize("path,expected", [
        # Brand-new artist folder: parent name IS the artist, lexicon empty.
        ("/Tapes/Oysterhead/2001-11-04 Hill Auditorium - Ann Arbor MI", "Oysterhead"),
        ("/Tapes/Frank Sinatra/1968-05-22 Oakland Coliseum", "Frank Sinatra"),
        # Deep wrapper: format containers are skipped past, not stopped at.
        ("/Tapes/Eric Clapton and Dr. John - 1996-01-13 - London/"
         "Eric Clapton and Dr. John - 1996-01-13 - London/Audio",
         "Eric Clapton and Dr. John"),
        ("/Tapes/Jeff Lynne's ELO - 3 Arena Dublin 25 October 2018/"
         "Jeff Lynne's ELO - 3 Arena Dublin 25 October 2018/flac files",
         "Jeff Lynne's ELO"),
        ("/Tapes/John Hammond/John Hammond- 1986-01-22 Nightstage, MA/FLAC/Acoustic Set",
         "John Hammond"),
        # Year-embedded ancestor names — cut at the year (handles both `\b`
        # boundary cases AND underscore-separated forms like "Black_Sabbath_1974").
        ("/Tapes/BHIC 1998.08.08 Camden (audience) [FLAC]/08.08.1998 Camden - Acoustic",
         "BHIC"),
        ("/Tapes/Black_Sabbath_1974-02-21/1974-02-01 Civic Arena, Pittsburgh, PA",
         "Black_Sabbath"),
        # `Various Artists` is recognised even though it lives in the
        # NOT_AN_ARTIST set (so the lexicon walk would normally stop).
        ("/Tapes/Various Artists/Cajun - Zydeco/Some Show", "Various Artists"),
    ])
    def test_recovers_artist_from_ancestor(self, path: str, expected: str):
        assert _trust_parent_artist(Path(path)) == expected

    @pytest.mark.parametrize("path", [
        # Date-prefixed parent — not an artist, walk up. Then "Tapes" is
        # NOT_AN_ARTIST so we stop with no answer.
        "/Tapes/2025_11_16_and_17_Bill_Graham_Civic_FLAC_Tagged/"
        "2025-11-16 Bill Graham Civic Auditorium, San Francisco, CA",
        # Bracketed/underscore-prefixed parent disqualifies.
        "/Tapes/[Unknown Album]/01 - Track01.flac",
        # Library-root word stops the walk.
        "/Tapes/2001-11-04 Hill Auditorium - Ann Arbor MI",
    ])
    def test_returns_none_when_no_clean_ancestor(self, path: str):
        assert _trust_parent_artist(Path(path)) is None


class TestLexiconArtistFromParent:
    """The ``_lexicon_artist_from_parent`` walk consults the lexicon at each
    ancestor. ``Various Artists`` is special-cased so a folder under
    ``Tapes/Various Artists/Concert For Amnesty/<show>/`` gets that label
    instead of stopping the walk on the NOT_AN_ARTIST hit."""

    def test_various_artists_ancestor_returns_label(self, tmp_path: Path):
        show = tmp_path / "Various Artists" / "Concert For Amnesty" / "Bob Geldof Set"
        show.mkdir(parents=True)
        # Empty lexicon — Various Artists recognition shouldn't depend on it.
        lex = Lexicon()
        assert _lexicon_artist_from_parent(show, lex) == "Various Artists"

    def test_known_artist_via_lexicon_match(self, tmp_path: Path):
        show = tmp_path / "Phil Lesh & Friends" / "1999-04-15 Warfield SF"
        show.mkdir(parents=True)
        lex = Lexicon()
        # Add to lexicon twice so it clears DEFAULT_MIN_COUNT=2.
        lex.add_artist("Phil Lesh & Friends")
        lex.add_artist("Phil Lesh & Friends")
        assert _lexicon_artist_from_parent(show, lex) == "Phil Lesh & Friends"

    def test_unknown_artist_returns_none(self, tmp_path: Path):
        show = tmp_path / "Some Brand New Artist" / "1999-04-15 Warfield SF"
        show.mkdir(parents=True)
        lex = Lexicon()
        assert _lexicon_artist_from_parent(show, lex) is None


# ---------------------------------------------------------------------------
# Regression tests built from real info.txt files the parser used to botch.
# Fixtures live under tests/fixtures/info_txt/ so the exact byte content is
# preserved (encoding, whitespace, etc.).

FIXTURES_TXT = Path(__file__).parent / "fixtures" / "info_txt"


class TestTajMahalBolognaFixture:
    """Line 1 is a rich ``Artist, City-Region, date, source`` header —
    perfect content, but the parser used to reject it (date + source noise)
    and fall through to a prose description two lines down, then stamp that
    sentence onto every FLAC."""

    def _body(self) -> str:
        return (FIXTURES_TXT / "taj_mahal_bologna.txt").read_text(encoding="utf-8")

    def test_artist_salvaged_from_rich_first_line(self):
        out = parse_info_txt(self._body())
        artist = (out.get("artist") or "").replace("MAHAL", "Mahal")
        assert artist.lower().startswith("taj mahal"), f"got {artist!r}"
        # Prose must never win.
        assert "interesting" not in artist.lower()
        assert "background" not in artist.lower()

    def test_setlist_recognized(self):
        titles = [t for _, t in parse_info_txt(self._body()).get("setlist", [])]
        assert "Sweet Home Chicago" in titles
        assert len(titles) == 23

    def test_build_concert_end_to_end(self, tmp_path: Path, make_flac):
        folder = tmp_path / (
            "Taj Mahal 1978-04-09 Bologna, Italy 2nd gen SB and 1978 filler"
        )
        folder.mkdir()
        for i in range(1, 24):
            make_flac(folder / f"{i:02d}.flac")
        audio = sorted(folder.glob("*.flac"))
        info = folder / "tajmahal78 info.txt"
        info.write_text(self._body(), encoding="utf-8")
        c = build_concert(folder, audio, info)
        assert c.artist and c.artist.lower().replace("mahal", "mahal").startswith("taj mahal"), \
            f"expected Taj Mahal, got {c.artist!r}"
        assert c.date == "1978-04-09"
        album = c.album_name()
        assert "interesting" not in album.lower()
        assert "background noise" not in album.lower()


class TestRichFirstLineSalvage:
    """`Artist, City-Region, date, source, ...` is a common etree / taper
    header shape. The artist lives before the first comma — salvage it
    even when the rest of the line trips source/date filters."""

    @pytest.mark.parametrize("line,expected", [
        ("Taj Mahal, Bologna-Italy, 9 april 1978, 2d gen SB + Bonus FM", "Taj Mahal"),
        ("Bob Dylan, Manchester UK, 17 May 1966, Free Trade Hall SBD", "Bob Dylan"),
        ("Phish, Madison Square Garden, December 31, 1997, MTX", "Phish"),
    ])
    def test_salvage_artist_before_first_comma(self, line, expected):
        body = f"{line}\n01. First\n02. Second\n"
        out = parse_info_txt(body)
        assert out.get("artist") == expected


class TestProseArtistFallsBackToParent:
    """When the body yields a prose-shaped artist (many words, sentence
    verbs), fall through to the parent folder rather than writing the
    prose string into every tag."""

    def test_parent_name_overrides_prose_body(self, tmp_path: Path, make_flac):
        folder = tmp_path / "Taj Mahal" / "1978-04-09 Bologna"
        folder.mkdir(parents=True)
        audio = [make_flac(folder / "01.flac")]
        # Body has no recognisable artist line; only prose descriptions.
        body = (
            "An interesting SB Taj solo, even there is some background noise.\n"
            "Richie Heavens came on stage for the last song.\n"
            "01. Sweet Home Chicago\n"
        )
        info = folder / "info.txt"
        info.write_text(body, encoding="utf-8")
        c = build_concert(folder, audio, info)
        assert c.artist == "Taj Mahal", f"got {c.artist!r}"

    @pytest.mark.parametrize("prose", [
        "An interesting SB Taj solo, even there is some background noise.",
        "This recording came from a friend in the 90s and sounds great.",
        "Richie Heavens came on stage for the last song of the night.",
        "Recorded from the soundboard with minor distortion throughout.",
    ])
    def test_parse_info_txt_rejects_prose_as_artist(self, prose):
        body = f"{prose}\n01. First Song\n"
        out = parse_info_txt(body)
        assert out.get("artist") != prose


class TestUnnumberedSetlist:
    """Qango, Kiss Brussels, and similar European tape info files list
    tracks one per line without numbering. Recognise them when an
    explicit ``Setlist``/``Tracklist`` header or disc marker precedes."""

    def test_tracks_under_setlist_header(self):
        body = (
            "QANGO - live at The Brook, Southampton\n"
            "Saturday 5th February, 2000\n"
            "\n"
            "Setlist:\n"
            "Intro (Fanfare)\n"
            "Sole Survivor\n"
            "Bitch's Crystal\n"
            "The Smile Has Left Your Eyes\n"
            "All Along the Watchtower\n"
        )
        out = parse_info_txt(body)
        titles = [t for _, t in out.get("setlist", [])]
        assert "Sole Survivor" in titles
        assert "Bitch's Crystal" in titles
        assert "All Along the Watchtower" in titles

    def test_tracks_under_disc_marker(self):
        body = (
            "Qango\n"
            "2000-02-05\n"
            "\n"
            "Disc 1\n"
            "\n"
            "Sole Survivor\n"
            "Bitch's Crystal\n"
            "All Along the Watchtower\n"
        )
        out = parse_info_txt(body)
        titles = [t for _, t in out.get("setlist", [])]
        assert titles == [
            "Sole Survivor",
            "Bitch's Crystal",
            "All Along the Watchtower",
        ]
