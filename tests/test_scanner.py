"""Unit tests for the single-scandir scanner."""
from __future__ import annotations

from pathlib import Path

import pytest

from tagcleaner.scanner import (
    DISC_FOLDER_RE,
    _classify,
    _enumerate_folder,
    _fingerprint,
    _is_multi_disc_parent,
    _parse_disc_marker,
    list_candidate_dirs,
    scan,
)


class TestClassify:
    def test_classifies_audio_txt_subdirs(self, tmp_path: Path, make_flac):
        folder = tmp_path / "show"
        folder.mkdir()
        a1 = make_flac(folder / "01 a.flac")
        a2 = make_flac(folder / "02 b.flac")
        info = folder / "show.txt"
        info.write_text("hello", encoding="utf-8")
        (folder / "nested").mkdir()
        (folder / ".DS_Store").write_text("x", encoding="utf-8")
        (folder / "._quarantine.flac").write_bytes(b"x")

        result = _classify(folder)
        assert result is not None
        audio, info_pairs, subdirs = result
        audio_names = [p.name for p, _ in audio]
        assert audio_names == ["01 a.flac", "02 b.flac"]
        info_names = [p.name for p, _ in info_pairs]
        assert info_names == ["show.txt"]
        assert [s.name for s in subdirs] == ["nested"]

    def test_fingerprint_hints_excluded(self, tmp_path: Path):
        folder = tmp_path / "show"
        folder.mkdir()
        (folder / "notes.txt").write_text("real", encoding="utf-8")
        (folder / "show.ffp.txt").write_text("fingerprint", encoding="utf-8")
        (folder / "show.md5.txt").write_text("md5", encoding="utf-8")
        result = _classify(folder)
        assert result is not None
        _, info_pairs, _ = result
        assert [p.name for p, _ in info_pairs] == ["notes.txt"]

    def test_unreadable_folder_returns_none(self, tmp_path: Path):
        assert _classify(tmp_path / "nonexistent") is None


class TestEnumerateFolder:
    def test_direct_audio(self, tmp_path: Path, make_flac):
        folder = tmp_path / "rush1984-09-21.sbd"
        folder.mkdir()
        make_flac(folder / "01 a.flac")
        make_flac(folder / "02 b.flac")
        (folder / "info.txt").write_text("Rush\n1984-09-21\n", encoding="utf-8")

        enum = _enumerate_folder(folder)
        assert enum is not None
        host, audio, info, fp = enum
        assert host == folder
        assert [p.name for p in audio] == ["01 a.flac", "02 b.flac"]
        assert info is not None and info.name == "info.txt"
        assert len(fp) == 40  # SHA-1 hex

    def test_nested_audio(self, tmp_path: Path, make_flac):
        folder = tmp_path / "show"
        inner = folder / "show"
        inner.mkdir(parents=True)
        make_flac(inner / "01 a.flac")
        (inner / "notes.txt").write_text("ok", encoding="utf-8")

        enum = _enumerate_folder(folder)
        assert enum is not None
        host, audio, info, _fp = enum
        assert host == inner
        assert [p.name for p in audio] == ["01 a.flac"]
        assert info is not None and info.name == "notes.txt"

    def test_largest_info_txt_wins(self, tmp_path: Path, make_flac):
        folder = tmp_path / "show"
        folder.mkdir()
        make_flac(folder / "01 a.flac")
        (folder / "small.txt").write_text("a", encoding="utf-8")
        (folder / "big.txt").write_text("long body " * 100, encoding="utf-8")
        enum = _enumerate_folder(folder)
        assert enum is not None
        _, _, info, _ = enum
        assert info is not None and info.name == "big.txt"

    def test_no_audio_returns_none(self, tmp_path: Path):
        folder = tmp_path / "empty"
        folder.mkdir()
        (folder / "readme.txt").write_text("nothing here", encoding="utf-8")
        assert _enumerate_folder(folder) is None

    def test_fingerprint_changes_with_content(self, tmp_path: Path, make_flac):
        folder = tmp_path / "show"
        folder.mkdir()
        make_flac(folder / "01 a.flac")
        fp1 = _enumerate_folder(folder)[3]
        # Add a new audio file.
        make_flac(folder / "02 b.flac")
        fp2 = _enumerate_folder(folder)[3]
        assert fp1 != fp2


class TestListCandidateDirs:
    def test_returns_mtime_pairs(self, tmp_path: Path, make_flac):
        a = tmp_path / "a"
        a.mkdir()
        make_flac(a / "01.flac")
        b = tmp_path / "b"
        b.mkdir()
        make_flac(b / "01.flac")
        (tmp_path / "file.txt").write_text("x", encoding="utf-8")
        (tmp_path / ".hidden").mkdir()
        out = list_candidate_dirs(tmp_path)
        names = [p.name for p, _ in out]
        assert names == ["a", "b"]
        for _, mtime in out:
            assert isinstance(mtime, float)
            assert mtime > 0

    def test_missing_root_returns_empty(self, tmp_path: Path):
        assert list_candidate_dirs(tmp_path / "does-not-exist") == []

    def test_artist_nested_library(self, tmp_path: Path, make_flac):
        # Tapes/Artist/concert/*.flac — the common organized-by-artist layout
        # that earlier versions of the scanner collapsed to one concert per
        # artist.
        for artist in ("Grateful Dead", "Phish"):
            for show in ("1987-08-22", "1993-02-10", "1997-12-31"):
                d = tmp_path / artist / f"{artist}-{show}"
                d.mkdir(parents=True)
                make_flac(d / "01.flac")
        out = list_candidate_dirs(tmp_path)
        names = sorted(p.name for p, _ in out)
        assert len(names) == 6
        assert "Grateful Dead-1987-08-22" in names
        assert "Phish-1997-12-31" in names

    def test_deep_nesting(self, tmp_path: Path, make_flac):
        # Tapes/Artist/Year/concert/*.flac
        d = tmp_path / "Grateful Dead" / "1987" / "gd1987-08-22"
        d.mkdir(parents=True)
        make_flac(d / "01.flac")
        d2 = tmp_path / "Grateful Dead" / "1988" / "gd1988-06-15"
        d2.mkdir(parents=True)
        make_flac(d2 / "01.flac")
        out = list_candidate_dirs(tmp_path)
        names = {p.name for p, _ in out}
        assert names == {"gd1987-08-22", "gd1988-06-15"}

    def test_nested_unpack_still_outer(self, tmp_path: Path, make_flac):
        # Classic archive-unpack: outer/outer/*.flac — outer is the concert.
        outer = tmp_path / "gd1987-08-22.sbd"
        inner = outer / "gd1987-08-22.sbd"
        inner.mkdir(parents=True)
        make_flac(inner / "01.flac")
        out = list_candidate_dirs(tmp_path)
        assert [p.name for p, _ in out] == ["gd1987-08-22.sbd"]

    def test_flat_layout_still_works(self, tmp_path: Path, make_flac):
        # Pending Cleanup style: Tapes/concert/*.flac
        for show in ("show1", "show2", "show3"):
            d = tmp_path / show
            d.mkdir()
            make_flac(d / "01.flac")
        out = list_candidate_dirs(tmp_path)
        assert [p.name for p, _ in out] == ["show1", "show2", "show3"]


class TestScan:
    def test_end_to_end(self, tmp_path: Path, make_concert_tree):
        make_concert_tree(
            "Talking Heads 1980-08-27 Wollman Rink",
            audio=["01 Psycho Killer.flac", "02 Warning Signs.flac"],
            info_txt=("info.txt", "Talking Heads\n1980-08-27\nSBD\n01. Psycho Killer\n02. Warning Signs\n"),
            root=tmp_path,
        )
        results = scan(tmp_path)
        assert len(results) == 1
        concert, fp, mtime = results[0]
        assert concert.artist == "Talking Heads"
        assert concert.date == "1980-08-27"
        assert isinstance(fp, str) and len(fp) == 40
        assert mtime > 0

    def test_pre_skip_short_circuits(self, tmp_path: Path, make_concert_tree):
        make_concert_tree(
            "show1", audio=["01.flac"], info_txt=("info.txt", "X\n1999-01-01\n01. a\n"), root=tmp_path,
        )
        make_concert_tree(
            "show2", audio=["01.flac"], info_txt=("info.txt", "Y\n1999-01-02\n01. a\n"), root=tmp_path,
        )
        seen: list[str] = []

        def pre_skip(folder: Path, mtime: float) -> bool:
            return folder.name == "show1"

        results = scan(
            tmp_path,
            pre_skip=pre_skip,
            on_skip=lambda p, i, t: seen.append(("skip", p.name)),
            on_done=lambda c, i, t: seen.append(("done", c.folder.name)),
        )
        assert len(results) == 1
        assert results[0][0].folder.name == "show2"
        assert ("skip", "show1") in seen
        assert ("done", "show2") in seen

    def test_skip_predicate_uses_fingerprint(self, tmp_path: Path, make_concert_tree):
        make_concert_tree(
            "show", audio=["01.flac"], info_txt=("info.txt", "X\n1999-01-01\n01. a\n"), root=tmp_path,
        )
        captured: list[tuple[str, str]] = []

        def skip(folder: Path, fp: str) -> bool:
            captured.append((folder.name, fp))
            return True

        results = scan(tmp_path, skip=skip)
        assert results == []
        assert len(captured) == 1
        assert captured[0][0] == "show"
        assert len(captured[0][1]) == 40

    def test_missing_root_returns_empty(self, tmp_path: Path):
        assert scan(tmp_path / "no-such-dir") == []


class TestMultiDiscSubfolders:
    """The scanner used to register each 'Disc 1' / 'Disc 2' subdir as its
    own concert candidate, which meant the parent info.txt never got paired
    with the audio and every disc came out at confidence 0.00."""

    @pytest.mark.parametrize("name", [
        "Disc 1", "Disc 2", "Disc 10", "disc 1", "disc1", "disc_01", "Disc.1",
        "CD 1", "CD 2", "cd2", "CD1", "cd_02",
        "DVD 1", "DVD2",
        "d1", "d2", "D01",
        "Disc One", "Disc Two", "Disc Three",
        "Set 1", "Set 2", "Set II", "set 1",
        "1st Set", "2nd Set", "3rd Set", "first set", "second set",
        "intermission", "intermission set",
        "Volume 1", "Volume 10", "Vol 2", "vol_3",
        "Encore", "encore", "Early Show", "Late Show", "Matinee",
    ])
    def test_disc_folder_re_matches(self, name):
        assert DISC_FOLDER_RE.match(name), name

    @pytest.mark.parametrize("name", [
        "1984-09-21 Disc 1",  # anchored: whole name must be disc marker
        "rush1984-09-21",
        "show",
        "disk1.5",             # not a clean integer
        "dataset",             # starts with 'd' but not 'd<digits>'
        "WHEN WE WERE KINGS [Disc 1]",  # bracketed — matched via _parse_disc_marker, not DISC_FOLDER_RE
    ])
    def test_disc_folder_re_rejects(self, name):
        assert not DISC_FOLDER_RE.match(name), name

    @pytest.mark.parametrize("name,base,idx", [
        ("Disc 1", "", 1),
        ("Disc 10", "", 10),
        ("disc_02", "", 2),
        ("Disc.1", "", 1),
        ("CD2", "", 2),
        ("DVD 3", "", 3),
        ("D1", "", 1),
        ("Disc One", "", 1),
        ("Set II", "", 2),
        ("1st Set", "", 1),
        ("first set", "", 1),
        ("Volume 4", "", 4),
        ("Vol 2", "", 2),
        ("Encore", "", 99),
        ("intermission set", "", 99),
        # bracketed / parenthesised disc markers
        ("[Disc 1]", "", 1),
        ("(Disc 2)", "", 2),
        ("(Disc.1)", "", 1),
        ("WHEN WE WERE KINGS [Disc 1]", "when we were kings", 1),
        ("DESTROYER (SODD) [Disc 1]", "destroyer sodd", 1),
        ("Hello Old Friend, Van (Disc 1)", "hello old friend, van", 1),
        ("16Bit (Disc.2)", "16bit", 2),
        # trailing format / qualifier
        ("Disc 1 Flac", "flac", 1),
        ("CD1 new", "new", 1),
        # trailing -D<n> suffix
        ("Dominion Theatre-London 2014-D1", "dominion theatre london 2014", 1),
    ])
    def test_parse_disc_marker(self, name, base, idx):
        parsed = _parse_disc_marker(name)
        assert parsed is not None, name
        assert parsed == (base, idx), name

    @pytest.mark.parametrize("name", [
        "BOOKER T 1",       # no keyword — shared-prefix path, not strict
        "rush1984-09-21",
        "show",
        "Outtakes",
        "1",                # bare digit — handled by shared-prefix path only
    ])
    def test_parse_disc_marker_none(self, name):
        assert _parse_disc_marker(name) is None, name

    def test_is_multi_disc_parent(self, tmp_path: Path, make_flac):
        parent = tmp_path / "show"
        (parent / "Disc 1").mkdir(parents=True)
        (parent / "Disc 2").mkdir(parents=True)
        make_flac(parent / "Disc 1" / "01.flac")
        make_flac(parent / "Disc 2" / "01.flac")
        subdirs = sorted((parent / n) for n in ("Disc 1", "Disc 2"))
        assert _is_multi_disc_parent(parent, subdirs)

    def test_is_multi_disc_parent_needs_audio_in_every_disc(self, tmp_path: Path, make_flac):
        parent = tmp_path / "show"
        (parent / "Disc 1").mkdir(parents=True)
        (parent / "Disc 2").mkdir(parents=True)
        make_flac(parent / "Disc 1" / "01.flac")
        # Disc 2 has no audio -- disqualifies the roll-up.
        subdirs = sorted((parent / n) for n in ("Disc 1", "Disc 2"))
        assert not _is_multi_disc_parent(parent, subdirs)

    def test_is_multi_disc_parent_rejects_mixed(self, tmp_path: Path, make_flac):
        parent = tmp_path / "show"
        (parent / "Disc 1").mkdir(parents=True)
        (parent / "extras").mkdir(parents=True)
        make_flac(parent / "Disc 1" / "01.flac")
        make_flac(parent / "extras" / "01.flac")
        subdirs = sorted((parent / n) for n in ("Disc 1", "extras"))
        assert not _is_multi_disc_parent(parent, subdirs)

    def test_is_multi_disc_parent_tolerates_artwork_peer(self, tmp_path: Path, make_flac):
        # Common case: parent has Disc 1 / Disc 2 / Artwork (no audio in Artwork).
        # Should still roll up.
        parent = tmp_path / "gd1990-12-12"
        for d in ("Disc 1", "Disc 2", "Artwork"):
            (parent / d).mkdir(parents=True)
        make_flac(parent / "Disc 1" / "01.flac")
        make_flac(parent / "Disc 2" / "01.flac")
        (parent / "Artwork" / "cover.jpg").write_bytes(b"\xff\xd8\xff\xe0")
        subdirs = sorted((parent / n) for n in ("Artwork", "Disc 1", "Disc 2"))
        assert _is_multi_disc_parent(parent, subdirs)

    def test_is_multi_disc_parent_shared_prefix_needs_concert_signal(
        self, tmp_path: Path, make_flac,
    ):
        # 'show1'/'show2'/'show3' siblings under a non-concert root must NOT
        # roll up — they're sibling concerts, not discs.
        for s in ("show1", "show2", "show3"):
            (tmp_path / s).mkdir()
            make_flac(tmp_path / s / "01.flac")
        subdirs = sorted(tmp_path / s for s in ("show1", "show2", "show3"))
        assert not _is_multi_disc_parent(tmp_path, subdirs)

    def test_is_multi_disc_parent_shared_prefix_with_date_in_parent(
        self, tmp_path: Path, make_flac,
    ):
        # Concert-shaped parent (date in name) lets shared-prefix sets through.
        parent = tmp_path / "BUDDY MILES 1972-05-22"
        for d in ("BOOKER T 1", "BOOKER T 2"):
            (parent / d).mkdir(parents=True)
            make_flac(parent / d / "01.flac")
        subdirs = sorted(parent / d for d in ("BOOKER T 1", "BOOKER T 2"))
        assert _is_multi_disc_parent(parent, subdirs)

    def test_is_multi_disc_parent_bare_digits_with_info_txt(
        self, tmp_path: Path, make_flac,
    ):
        parent = tmp_path / "concert"
        (parent / "1").mkdir(parents=True)
        (parent / "2").mkdir(parents=True)
        make_flac(parent / "1" / "01.flac")
        make_flac(parent / "2" / "01.flac")
        (parent / "info.txt").write_text("Phish\n1997-12-31\n", encoding="utf-8")
        subdirs = sorted(parent / d for d in ("1", "2"))
        assert _is_multi_disc_parent(parent, subdirs)

    def test_multi_disc_rolls_up_to_parent(self, tmp_path: Path, make_flac):
        parent = tmp_path / "gd1990-12-12"
        (parent / "Disc 1").mkdir(parents=True)
        (parent / "Disc 2").mkdir(parents=True)
        make_flac(parent / "Disc 1" / "01.flac")
        make_flac(parent / "Disc 1" / "02.flac")
        make_flac(parent / "Disc 2" / "01.flac")
        (parent / "info.txt").write_text(
            "Grateful Dead\n1990-12-12\nSet 1:\n01. a\n02. b\nSet 2:\n01. c\n",
            encoding="utf-8",
        )
        out = list_candidate_dirs(tmp_path)
        assert [p.name for p, _ in out] == ["gd1990-12-12"]

    def test_multi_disc_enumerate_aggregates_audio(self, tmp_path: Path, make_flac):
        parent = tmp_path / "gd1990-12-12"
        (parent / "Disc 1").mkdir(parents=True)
        (parent / "Disc 2").mkdir(parents=True)
        make_flac(parent / "Disc 1" / "01.flac")
        make_flac(parent / "Disc 1" / "02.flac")
        make_flac(parent / "Disc 2" / "01.flac")
        (parent / "info.txt").write_text("hi", encoding="utf-8")

        enum = _enumerate_folder(parent)
        assert enum is not None
        host, audio, info, fp = enum
        assert host == parent
        # Disc 1 files first (alphanumeric disc order), then Disc 2.
        assert [p.relative_to(parent).as_posix() for p in audio] == [
            "Disc 1/01.flac", "Disc 1/02.flac", "Disc 2/01.flac",
        ]
        assert info is not None and info.name == "info.txt"
        assert len(fp) == 40

    def test_multi_disc_enumerate_inherits_info_from_disc(self, tmp_path: Path, make_flac):
        """Parent has no info.txt, but Disc 1 does. Use the Disc 1 one."""
        parent = tmp_path / "show"
        (parent / "Disc 1").mkdir(parents=True)
        (parent / "Disc 2").mkdir(parents=True)
        make_flac(parent / "Disc 1" / "01.flac")
        make_flac(parent / "Disc 2" / "01.flac")
        (parent / "Disc 1" / "notes.txt").write_text("x", encoding="utf-8")
        enum = _enumerate_folder(parent)
        assert enum is not None
        _, _, info, _ = enum
        assert info is not None and info.name == "notes.txt"

    def test_multi_disc_ordering_numeric_not_lexical(self, tmp_path: Path, make_flac):
        parent = tmp_path / "show"
        for d in ("Disc 1", "Disc 2", "Disc 10"):
            (parent / d).mkdir(parents=True)
            make_flac(parent / d / "01.flac")
        enum = _enumerate_folder(parent)
        assert enum is not None
        _, audio, _, _ = enum
        disc_order = [p.parent.name for p in audio]
        assert disc_order == ["Disc 1", "Disc 2", "Disc 10"]

    def test_multi_disc_fingerprint_disambiguates_same_filenames(
        self, tmp_path: Path, make_flac,
    ):
        """Both discs have 01.flac with the same size. Old fingerprint would
        collapse them to one entry; new one must not."""
        single = tmp_path / "single"
        single.mkdir()
        make_flac(single / "01.flac")

        multi = tmp_path / "multi"
        (multi / "Disc 1").mkdir(parents=True)
        (multi / "Disc 2").mkdir(parents=True)
        make_flac(multi / "Disc 1" / "01.flac")
        make_flac(multi / "Disc 2" / "01.flac")

        enum_single = _enumerate_folder(single)
        enum_multi = _enumerate_folder(multi)
        assert enum_single is not None and enum_multi is not None
        assert enum_single[3] != enum_multi[3]

    def test_single_disc_folder_not_rolled_up(self, tmp_path: Path, make_flac):
        """A lone 'Disc 1' folder under the root is still a concert
        candidate on its own -- we only roll up when the parent has 2+
        disc-named children."""
        root = tmp_path
        d = root / "Disc 1"
        d.mkdir()
        make_flac(d / "01.flac")
        out = list_candidate_dirs(root)
        assert [p.name for p, _ in out] == ["Disc 1"]

    def test_format_wrapper_collapses_to_parent(self, tmp_path: Path, make_flac):
        """parent/FLAC/audio.flac unpack pattern -- treat parent as the
        concert (the FLAC subfolder is just a format bucket)."""
        parent = tmp_path / "Black Sabbath - Tokyo 1980-11-16"
        flac_dir = parent / "FLAC"
        flac_dir.mkdir(parents=True)
        make_flac(flac_dir / "01.flac")
        out = list_candidate_dirs(tmp_path)
        assert [p.name for p, _ in out] == ["Black Sabbath - Tokyo 1980-11-16"]

    def test_artwork_peer_lets_disc_set_roll_up(self, tmp_path: Path, make_flac):
        parent = tmp_path / "show"
        for d in ("Disc 1", "Disc 2"):
            (parent / d).mkdir(parents=True)
            make_flac(parent / d / "01.flac")
        (parent / "Scans").mkdir()  # no audio inside
        out = list_candidate_dirs(tmp_path)
        assert [p.name for p, _ in out] == ["show"]

    @pytest.mark.parametrize("disc_names", [
        ("[Disc 1]", "[Disc 2]"),
        ("(Disc 1)", "(Disc 2)"),
        ("(Disc.1)", "(Disc.2)"),
        ("WHEN WE WERE KINGS [Disc 1]", "WHEN WE WERE KINGS [Disc 2]"),
        ("DESTROYER (SODD) [Disc 1]", "DESTROYER (SODD) [Disc 2]"),
        ("Hello Old Friend, Van (Disc 1)", "Hello Old Friend, Van (Disc 2)"),
        ("16Bit (Disc.1)", "16Bit (Disc.2)"),
        ("Disc 1 Flac", "Disc 2 Flac"),
        ("CD1 new", "CD2 new"),
        ("Volume 1", "Volume 2"),
        ("Vol 1", "Vol 2"),
        ("1st Set", "2nd Set"),
        ("first set", "second set"),
        ("Dominion Theatre-London 2014-D1", "Dominion Theatre-London 2014-D2"),
    ])
    def test_bracket_and_suffix_variants_roll_up(
        self, tmp_path: Path, make_flac, disc_names,
    ):
        parent = tmp_path / "show"
        for d in disc_names:
            (parent / d).mkdir(parents=True)
            make_flac(parent / d / "01.flac")
        out = list_candidate_dirs(tmp_path)
        assert [p.name for p, _ in out] == ["show"], disc_names

    def test_mismatched_bracket_prefixes_do_not_roll_up(
        self, tmp_path: Path, make_flac,
    ):
        # Two different shows with disc markers must NOT collapse into one —
        # the text outside the marker (the "base") has to match.
        parent = tmp_path / "box"
        for d in ("Berlin 1976 [Disc 1]", "Tokyo 1977 [Disc 1]"):
            (parent / d).mkdir(parents=True)
            make_flac(parent / d / "01.flac")
        out = sorted(p.name for p, _ in list_candidate_dirs(tmp_path))
        assert out == ["Berlin 1976 [Disc 1]", "Tokyo 1977 [Disc 1]"]

    def test_single_cd_wrapper_collapses_to_parent(self, tmp_path: Path, make_flac):
        parent = tmp_path / "Zeppelin 1975-03-12"
        inner = parent / "Single CD"
        inner.mkdir(parents=True)
        make_flac(inner / "01.flac")
        out = list_candidate_dirs(tmp_path)
        assert [p.name for p, _ in out] == ["Zeppelin 1975-03-12"]

    def test_cd_wrapper_collapses_to_parent(self, tmp_path: Path, make_flac):
        parent = tmp_path / "Zeppelin 1975-03-12"
        inner = parent / "CD"
        inner.mkdir(parents=True)
        make_flac(inner / "01.flac")
        out = list_candidate_dirs(tmp_path)
        assert [p.name for p, _ in out] == ["Zeppelin 1975-03-12"]

    def test_volume_series_ordering(self, tmp_path: Path, make_flac):
        # Volume 1..Volume 10 should be aggregated in numeric order.
        parent = tmp_path / "boxset"
        for n in (1, 2, 10):
            (parent / f"Volume {n}").mkdir(parents=True)
            make_flac(parent / f"Volume {n}" / "01.flac")
        enum = _enumerate_folder(parent)
        assert enum is not None
        _, audio, _, _ = enum
        order = [p.parent.name for p in audio]
        assert order == ["Volume 1", "Volume 2", "Volume 10"]


class TestFingerprint:
    def test_deterministic(self, tmp_path: Path):
        pairs = [(Path("01.flac"), 10), (Path("02.flac"), 20)]
        info = [(Path("notes.txt"), 100)]
        a = _fingerprint("show", pairs, info)
        b = _fingerprint("show", pairs, info)
        assert a == b

    def test_order_independent(self, tmp_path: Path):
        pairs_a = [(Path("01.flac"), 10), (Path("02.flac"), 20)]
        pairs_b = list(reversed(pairs_a))
        info = [(Path("n.txt"), 50)]
        assert _fingerprint("x", pairs_a, info) == _fingerprint("x", pairs_b, info)

    def test_size_change_changes_fingerprint(self, tmp_path: Path):
        info = [(Path("n.txt"), 50)]
        a = _fingerprint("x", [(Path("01.flac"), 10)], info)
        b = _fingerprint("x", [(Path("01.flac"), 11)], info)
        assert a != b

    def test_folder_name_changes_fingerprint(self, tmp_path: Path):
        pairs = [(Path("01.flac"), 10)]
        assert _fingerprint("a", pairs, []) != _fingerprint("b", pairs, [])
