"""Unit tests for the single-scandir scanner."""
from __future__ import annotations

from pathlib import Path

import pytest

from tagcleaner.scanner import (
    _classify,
    _enumerate_folder,
    _fingerprint,
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
