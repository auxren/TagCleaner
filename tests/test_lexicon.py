"""Tests for the artist/venue lexicon and its parser integration."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tagcleaner.lexicon import Lexicon, normalize_name
from tagcleaner.parser import build_concert


class TestNormalize:
    @pytest.mark.parametrize("raw,expected", [
        ("Grateful Dead", "grateful dead"),
        ("  The Talking Heads  ", "talking heads"),
        ("Phish!", "phish"),
        ("R.E.M.", "r e m"),
        ("Gov't Mule", "gov t mule"),
        ("Björk", "björk"),
        ("", ""),
        ("   ", ""),
        ("The The", "the"),  # "The " prefix strips once
    ])
    def test_cases(self, raw, expected):
        assert normalize_name(raw) == expected


class TestBuildFromConcertDicts:
    def test_counts_artists_and_venues(self):
        dicts = [
            {"artist": "Grateful Dead", "venue": "Fillmore East"},
            {"artist": "Grateful Dead", "venue": "Fillmore East"},
            {"artist": "Phish", "venue": "Madison Square Garden"},
            {"artist": None, "venue": None},
            {"artist": "", "venue": ""},
        ]
        lex = Lexicon.from_concert_dicts(dicts)
        assert lex.artists == {"Grateful Dead": 2, "Phish": 1}
        assert lex.venues == {"Fillmore East": 2, "Madison Square Garden": 1}

    def test_case_folding_keeps_most_common_spelling(self):
        dicts = [
            {"artist": "grateful dead", "venue": None},
            {"artist": "Grateful Dead", "venue": None},
            {"artist": "Grateful Dead", "venue": None},
            {"artist": "GRATEFUL DEAD", "venue": None},
        ]
        lex = Lexicon.from_concert_dicts(dicts)
        # "Grateful Dead" is the most-seen spelling; count is the sum of all.
        assert lex.artists == {"Grateful Dead": 4}

    def test_the_prefix_folds_together(self):
        dicts = [
            {"artist": "The Band"},
            {"artist": "Band"},
            {"artist": "The Band"},
        ]
        lex = Lexicon.from_concert_dicts(dicts)
        assert sum(lex.artists.values()) == 3
        # Canonical should be the more-common spelling.
        assert "The Band" in lex.artists


class TestMatchArtist:
    def test_exact_match(self):
        lex = Lexicon(artists={"Grateful Dead": 10})
        assert lex.match_artist("Grateful Dead") == "Grateful Dead"

    def test_case_insensitive_returns_canonical(self):
        lex = Lexicon(artists={"Grateful Dead": 10})
        assert lex.match_artist("grateful dead") == "Grateful Dead"
        assert lex.match_artist("GRATEFUL DEAD") == "Grateful Dead"

    def test_the_prefix_matches(self):
        lex = Lexicon(artists={"Talking Heads": 5})
        assert lex.match_artist("The Talking Heads") == "Talking Heads"

    def test_fuzzy_match(self):
        lex = Lexicon(artists={"Grateful Dead": 10})
        # One-character typo inside a long name — difflib fuzzy catches it.
        assert lex.match_artist("Grateful Deed") == "Grateful Dead"

    def test_below_min_count_rejected(self):
        lex = Lexicon(artists={"Obscure Band": 1})
        assert lex.match_artist("Obscure Band") is None
        assert lex.match_artist("Obscure Band", min_count=1) == "Obscure Band"

    def test_unknown_returns_none(self):
        lex = Lexicon(artists={"Grateful Dead": 10})
        assert lex.match_artist("Pink Floyd") is None

    def test_empty_candidate_returns_none(self):
        lex = Lexicon(artists={"Grateful Dead": 10})
        assert lex.match_artist(None) is None
        assert lex.match_artist("") is None
        assert lex.match_artist("   ") is None

    def test_short_candidate_skips_fuzzy(self):
        # Only exact match for short strings — "moe" shouldn't fuzzy-match
        # "doe" or similar near-misses.
        lex = Lexicon(artists={"moe.": 10})
        assert lex.match_artist("moe") == "moe."  # normalizes the same
        assert lex.match_artist("doe") is None


class TestMatchVenue:
    def test_exact_and_fuzzy(self):
        lex = Lexicon(venues={"Fillmore East": 3, "Madison Square Garden": 5})
        assert lex.match_venue("fillmore east") == "Fillmore East"
        assert lex.match_venue("Fillmore Eest") == "Fillmore East"  # typo
        assert lex.match_venue("Wollman Rink") is None


class TestAdd:
    def test_add_new_artist(self):
        lex = Lexicon()
        canon = lex.add_artist("Black Sabbath")
        assert canon == "Black Sabbath"
        assert lex.artists == {"Black Sabbath": 1}
        # Match now returns it with default min_count=2? No — count is 1.
        assert lex.match_artist("Black Sabbath") is None
        # Adding again bumps count past the threshold.
        lex.add_artist("Black Sabbath")
        assert lex.match_artist("Black Sabbath") == "Black Sabbath"

    def test_add_count_boost(self):
        """A batch answer (N siblings all the same artist) can boost past
        the min-count threshold in a single call."""
        lex = Lexicon()
        lex.add_artist("Black Sabbath", count=5)
        assert lex.artists == {"Black Sabbath": 5}
        assert lex.match_artist("Black Sabbath") == "Black Sabbath"

    def test_add_merges_case_variants(self):
        lex = Lexicon(artists={"black sabbath": 1})
        canon = lex.add_artist("Black Sabbath", count=3)
        # The new, more-common Title Case form wins as canonical.
        assert canon == "Black Sabbath"
        assert "black sabbath" not in lex.artists
        assert lex.artists["Black Sabbath"] == 4

    def test_add_rejects_blank(self):
        lex = Lexicon()
        with pytest.raises(ValueError):
            lex.add_artist("")
        with pytest.raises(ValueError):
            lex.add_artist("   ")

    def test_add_venue(self):
        lex = Lexicon()
        lex.add_venue("Madison Square Garden", count=2)
        assert lex.match_venue("madison square garden") == "Madison Square Garden"


class TestSaveLoad:
    def test_roundtrip(self, tmp_path: Path):
        lex = Lexicon(
            artists={"Grateful Dead": 42, "Phish": 17},
            venues={"Fillmore East": 5},
        )
        path = tmp_path / "lex.json"
        lex.save(path)
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert raw["schema"] == 1
        # Saved in count-desc order for human inspection.
        assert list(raw["artists"].keys())[0] == "Grateful Dead"

        loaded = Lexicon.load(path)
        assert loaded.artists == lex.artists
        assert loaded.venues == lex.venues

    def test_load_missing_file(self, tmp_path: Path):
        lex = Lexicon.load(tmp_path / "does-not-exist.json")
        assert lex.artists == {}
        assert lex.venues == {}

    def test_load_wrong_schema(self, tmp_path: Path):
        path = tmp_path / "lex.json"
        path.write_text(json.dumps({"schema": 99, "artists": {"x": 1}}))
        lex = Lexicon.load(path)
        assert lex.artists == {}


class TestParserIntegration:
    def test_parent_folder_fallback_uses_lexicon(
        self, tmp_path: Path, make_flac,
    ):
        # Date-first folder inside a parent whose name the lexicon knows.
        parent = tmp_path / "Black Sabbath"
        show = parent / "1969-12-17 Chestnut Cabaret"
        make_flac(show / "01.flac")
        lex = Lexicon(artists={"Black Sabbath": 50})

        concert = build_concert(show, [show / "01.flac"], None, lexicon=lex)
        assert concert.artist == "Black Sabbath"

    def test_parent_folder_fallback_requires_lexicon_confirmation(
        self, tmp_path: Path, make_flac,
    ):
        # Same layout, but the lexicon has no entry for the parent name.
        parent = tmp_path / "Some Random Folder"
        show = parent / "1969-12-17 Show"
        make_flac(show / "01.flac")
        lex = Lexicon(artists={"Other Band": 10})

        concert = build_concert(show, [show / "01.flac"], None, lexicon=lex)
        assert concert.artist is None

    def test_parent_rejects_non_artist_names(
        self, tmp_path: Path, make_flac,
    ):
        # "Tapes" is in the reject list, so even if the lexicon happened to
        # have a "Tapes" entry we wouldn't adopt it.
        parent = tmp_path / "Tapes"
        show = parent / "1969-12-17 Show"
        make_flac(show / "01.flac")
        lex = Lexicon(artists={"Tapes": 10})

        concert = build_concert(show, [show / "01.flac"], None, lexicon=lex)
        assert concert.artist is None

    def test_parent_rejects_year_only(self, tmp_path: Path, make_flac):
        parent = tmp_path / "1987"
        show = parent / "1987-12-17 Show"
        make_flac(show / "01.flac")
        lex = Lexicon(artists={"1987": 10})

        concert = build_concert(show, [show / "01.flac"], None, lexicon=lex)
        assert concert.artist is None

    def test_canonicalizes_parsed_artist(self, tmp_path: Path, make_flac):
        # Parser picks up "grateful dead" from the folder name; lexicon
        # upgrades it to the canonical capitalised form.
        show = tmp_path / "grateful dead 1977-05-08 Barton Hall"
        make_flac(show / "01.flac")
        lex = Lexicon(artists={"Grateful Dead": 100})

        concert = build_concert(show, [show / "01.flac"], None, lexicon=lex)
        assert concert.artist == "Grateful Dead"

    def test_canonicalizes_parsed_venue(self, tmp_path: Path, make_flac):
        # Parser sees "Madison Square Gardens" (one-character typo); the
        # lexicon knows the canonical "Madison Square Garden" and fuzzy
        # upgrades the spelling.
        show = tmp_path / "Phish 1997-11-17"
        make_flac(show / "01.flac")
        info = show / "info.txt"
        info.parent.mkdir(parents=True, exist_ok=True)
        info.write_text(
            "Phish\n1997-11-17\nMadison Square Gardens\nNew York, NY\n",
            encoding="utf-8",
        )
        lex = Lexicon(venues={"Madison Square Garden": 20})

        concert = build_concert(show, [show / "01.flac"], info, lexicon=lex)
        assert concert.venue == "Madison Square Garden"

    def test_no_lexicon_means_no_change(self, tmp_path: Path, make_flac):
        parent = tmp_path / "Black Sabbath"
        show = parent / "1969-12-17 Show"
        make_flac(show / "01.flac")

        concert = build_concert(show, [show / "01.flac"], None, lexicon=None)
        assert concert.artist is None
