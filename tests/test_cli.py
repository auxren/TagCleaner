"""End-to-end CLI tests: dry-run, in-place, history skip, rescan-all."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from mutagen.flac import FLAC

from tagcleaner.cli import _track_tolerance, main


@pytest.fixture
def library(tmp_path: Path, make_concert_tree):
    """A small library with two concert folders in deterministic shapes."""
    make_concert_tree(
        "Talking Heads 1980-08-27 Wollman Rink",
        audio=["01 Psycho Killer.flac", "02 Warning Signs.flac"],
        info_txt=(
            "info.txt",
            "Talking Heads\n"
            "1980-08-27\n"
            "Wollman Rink\n"
            "New York, NY\n"
            "Soundboard\n"
            "\n"
            "01. Psycho Killer\n"
            "02. Warning Signs\n",
        ),
        root=tmp_path,
    )
    make_concert_tree(
        "rush1984-09-21.sbd",
        audio=["01 Spirit of Radio.flac"],
        info_txt=(
            "info.txt",
            "Rush\n"
            "1984-09-21\n"
            "Maple Leaf Gardens\n"
            "Toronto, Canada\n"
            "SBD\n"
            "\n"
            "01. The Spirit of Radio\n",
        ),
        root=tmp_path,
    )
    return tmp_path


class TestDryRun:
    def test_writes_drafts_and_does_not_tag(self, library: Path):
        code = main([str(library), "--dry-run", "--no-banner", "--yes"])
        assert code == 0
        drafts_path = library / "tagcleaner-drafts.json"
        assert drafts_path.exists()
        drafts = json.loads(drafts_path.read_text(encoding="utf-8"))
        assert len(drafts) == 2
        # No tags should have been written to any audio file.
        for f in library.rglob("*.flac"):
            assert "ARTIST" not in FLAC(str(f))


class TestInPlace:
    def test_tags_written(self, library: Path):
        code = main([str(library), "--yes", "--no-banner"])
        assert code == 0
        th = library / "Talking Heads 1980-08-27 Wollman Rink" / "01 Psycho Killer.flac"
        f = FLAC(str(th))
        assert f["ARTIST"] == ["Talking Heads"]
        assert f["TITLE"] == ["Psycho Killer"]
        assert f["ALBUM"] and f["ALBUM"][0].startswith("1980-08-27 ")

    def test_history_written(self, library: Path):
        main([str(library), "--yes", "--no-banner"])
        hist_path = library / "tagcleaner-history.json"
        assert hist_path.exists()
        data = json.loads(hist_path.read_text(encoding="utf-8"))
        assert data["schema"] == 1
        assert len(data["entries"]) == 2
        for entry in data["entries"].values():
            assert entry["tagging"]["mode"] == "in-place"
            assert entry["tagging"]["applied"] >= 1
            assert "folder_mtime" in entry


class TestHistorySkip:
    def test_second_run_skips_unchanged_folders(self, library: Path, capsys):
        main([str(library), "--yes", "--no-banner"])
        capsys.readouterr()  # clear
        code = main([str(library), "--yes", "--no-banner"])
        assert code == 0
        out = capsys.readouterr().out
        # No "fresh" concerts, everything is skipped.
        assert "skipped" in out.lower() or "cached" in out.lower()

    def test_rescan_all_reprocesses(self, library: Path):
        main([str(library), "--yes", "--no-banner"])
        code = main([str(library), "--yes", "--no-banner", "--rescan-all"])
        assert code == 0
        # History still exists and still has 2 entries.
        hist = json.loads((library / "tagcleaner-history.json").read_text(encoding="utf-8"))
        assert len(hist["entries"]) == 2

    def test_no_history_flag_skips_file(self, library: Path):
        code = main([str(library), "--dry-run", "--no-banner", "--yes", "--no-history"])
        assert code == 0
        assert not (library / "tagcleaner-history.json").exists()

    def test_content_change_triggers_reparse(self, library: Path, make_flac):
        main([str(library), "--yes", "--no-banner"])
        # Add a new audio file to one folder; mtime + fingerprint both change.
        folder = library / "rush1984-09-21.sbd"
        make_flac(folder / "02 New Track.flac")
        # Bump mtime in case FS resolution misses the change.
        os.utime(folder, None)

        main([str(library), "--dry-run", "--no-banner", "--yes"])
        # The rush folder should appear in new drafts since its fingerprint changed.
        drafts = json.loads((library / "tagcleaner-drafts.json").read_text(encoding="utf-8"))
        folders = [d["folder"] for d in drafts]
        assert any("rush1984" in f for f in folders)


class TestCopyTo:
    def test_copies_and_tags(self, library: Path, tmp_path: Path):
        dst = tmp_path / "tagged"
        code = main([str(library), "--copy-to", str(dst), "--yes", "--no-banner"])
        assert code == 0
        # Source audio is untouched.
        src = library / "rush1984-09-21.sbd" / "01 Spirit of Radio.flac"
        assert "ARTIST" not in FLAC(str(src))
        # Copy is tagged.
        dst_file = dst / "rush1984-09-21.sbd" / "01 Spirit of Radio.flac"
        assert dst_file.exists()
        f = FLAC(str(dst_file))
        assert f["ARTIST"] == ["Rush"]


class TestPlainUI:
    def test_plain_scan_runs(self, library: Path):
        code = main([str(library), "--dry-run", "--no-banner", "--yes", "--plain"])
        assert code == 0
        assert (library / "tagcleaner-drafts.json").exists()

    def test_plain_in_place_writes_tags(self, library: Path):
        from mutagen.flac import FLAC
        code = main([str(library), "--yes", "--no-banner", "--plain"])
        assert code == 0
        th = library / "Talking Heads 1980-08-27 Wollman Rink" / "01 Psycho Killer.flac"
        assert FLAC(str(th))["ARTIST"] == ["Talking Heads"]


class TestTrackTolerance:
    @pytest.mark.parametrize("tracks,files,override,expected", [
        # auto mode: max(2, ceil(0.15 * min))
        (10, 10, -1, 2),
        (10, 11, -1, 2),     # shorter=10, ceil(1.5)=2
        (25, 24, -1, 4),     # shorter=24, ceil(3.6)=4
        (50, 30, -1, 5),     # shorter=30, ceil(4.5)=5
        (8, 6, -1, 2),       # shorter=6, ceil(0.9)=1, floor at 2
        (100, 100, -1, 15),  # shorter=100, ceil(15.0)=15
        # strict
        (10, 11, 0, 0),
        # explicit override
        (10, 11, 3, 3),
        (50, 50, 7, 7),
    ])
    def test_helper(self, tracks, files, override, expected):
        assert _track_tolerance(tracks, files, override) == expected

    def test_partial_tag_when_within_tolerance(
        self, tmp_path: Path, make_concert_tree,
    ):
        # info.txt lists 3 tracks, only 2 audio files — off by 1, within auto
        # tolerance. Must tag both files rather than skip the whole concert.
        make_concert_tree(
            "Rush 1984-09-21 Maple Leaf Gardens",
            audio=["01 a.flac", "02 b.flac"],
            info_txt=(
                "info.txt",
                "Rush\n1984-09-21\nMaple Leaf Gardens\nToronto, Canada\nSBD\n\n"
                "01. Spirit of Radio\n02. Tom Sawyer\n03. Encore: YYZ\n",
            ),
            root=tmp_path,
        )
        code = main([str(tmp_path), "--yes", "--no-banner"])
        assert code == 0
        f = FLAC(str(tmp_path / "Rush 1984-09-21 Maple Leaf Gardens" / "01 a.flac"))
        assert f["ARTIST"] == ["Rush"]
        assert f["TITLE"] == ["Spirit of Radio"]

    def test_strict_flag_restores_old_behaviour(
        self, tmp_path: Path, make_concert_tree,
    ):
        make_concert_tree(
            "Rush 1984-09-21",
            audio=["01 a.flac", "02 b.flac"],
            info_txt=(
                "info.txt",
                "Rush\n1984-09-21\nMaple Leaf Gardens\nToronto, Canada\nSBD\n\n"
                "01. Spirit of Radio\n02. Tom Sawyer\n03. Encore\n",
            ),
            root=tmp_path,
        )
        code = main([str(tmp_path), "--yes", "--no-banner", "--track-tolerance", "0"])
        assert code == 0
        # Strict mode still applies concert-level metadata via the metadata-only
        # fallback; per-track TITLE/TRACKNUMBER stay unwritten.
        f = FLAC(str(tmp_path / "Rush 1984-09-21" / "01 a.flac"))
        assert f["ARTIST"] == ["Rush"]
        assert "TITLE" not in f
        assert "TRACKNUMBER" not in f

    def test_large_mismatch_falls_back_to_metadata_only(
        self, tmp_path: Path, make_concert_tree,
    ):
        # 5 tracks in info.txt, 1 audio file — wildly past auto tolerance.
        # Concert-level metadata still gets stamped, per-track tags do not.
        make_concert_tree(
            "Phish 1997-11-17 MSG",
            audio=["show.flac"],
            info_txt=(
                "info.txt",
                "Phish\n1997-11-17\nMadison Square Garden\nNew York, NY\nAUD\n\n"
                "01. Ghost\n02. Wolfman's Brother\n03. Stash\n"
                "04. Bathtub Gin\n05. Character Zero\n",
            ),
            root=tmp_path,
        )
        code = main([str(tmp_path), "--yes", "--no-banner"])
        assert code == 0
        f = FLAC(str(tmp_path / "Phish 1997-11-17 MSG" / "show.flac"))
        assert f["ARTIST"] == ["Phish"]
        assert f["DATE"] == ["1997-11-17"]
        assert "TITLE" not in f
        assert "TRACKNUMBER" not in f


class TestPromptUnknown:
    """The --prompt-unknown flag asks the user to fill in missing artists
    and feeds the answers into the lexicon so later runs don't re-ask."""

    def test_answers_fill_artists_and_feed_lexicon(
        self, tmp_path: Path, make_concert_tree, monkeypatch,
    ):
        # Two date-first folders with no info.txt — parser has no signal
        # to guess an artist, so both land in the prompt queue.
        make_concert_tree("1969-12-17 Show A", audio=["01 a.flac"], root=tmp_path)
        make_concert_tree("1970-01-05 Show B", audio=["01 b.flac"], root=tmp_path)

        # Two concerts live under the same parent (tmp_path), so the prompt
        # collapses them into one question. The user types "Black Sabbath".
        answers = iter(["Black Sabbath\n"])
        monkeypatch.setattr("builtins.input", lambda _p="": next(answers).rstrip("\n"))
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)

        code = main([str(tmp_path), "--dry-run", "--no-banner", "--yes", "--prompt-unknown"])
        assert code == 0

        lex_path = tmp_path / "tagcleaner-lexicon.json"
        assert lex_path.exists()
        lex = json.loads(lex_path.read_text(encoding="utf-8"))
        assert lex["artists"]["Black Sabbath"] >= 2

        drafts = json.loads((tmp_path / "tagcleaner-drafts.json").read_text(encoding="utf-8"))
        assert all(d["artist"] == "Black Sabbath" for d in drafts)

    def test_empty_answer_skips_group(
        self, tmp_path: Path, make_concert_tree, monkeypatch,
    ):
        make_concert_tree("1969-12-17 Show", audio=["01 a.flac"], root=tmp_path)
        answers = iter(["\n"])
        monkeypatch.setattr("builtins.input", lambda _p="": next(answers).rstrip("\n"))
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)

        code = main([str(tmp_path), "--dry-run", "--no-banner", "--yes", "--prompt-unknown"])
        assert code == 0
        drafts = json.loads((tmp_path / "tagcleaner-drafts.json").read_text(encoding="utf-8"))
        assert drafts[0]["artist"] is None

    def test_quit_stops_asking(
        self, tmp_path: Path, make_concert_tree, monkeypatch,
    ):
        # Three no-artist concerts under different parents so they form
        # three separate prompt groups. 'q' on the first stops the rest.
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        (tmp_path / "c").mkdir()
        make_concert_tree("1969-12-17 Show", audio=["01 a.flac"], root=tmp_path / "a")
        make_concert_tree("1970-01-05 Show", audio=["01 b.flac"], root=tmp_path / "b")
        make_concert_tree("1971-03-12 Show", audio=["01 c.flac"], root=tmp_path / "c")
        answers = iter(["q\n"])
        monkeypatch.setattr("builtins.input", lambda _p="": next(answers).rstrip("\n"))
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)

        code = main([str(tmp_path), "--dry-run", "--no-banner", "--yes", "--prompt-unknown"])
        assert code == 0
        # Only the first prompt was shown, then we bailed; nothing got tagged.
        drafts = json.loads((tmp_path / "tagcleaner-drafts.json").read_text(encoding="utf-8"))
        assert all(d["artist"] is None for d in drafts)

    def test_not_tty_does_not_prompt(
        self, tmp_path: Path, make_concert_tree, monkeypatch,
    ):
        make_concert_tree("1969-12-17 Show", audio=["01 a.flac"], root=tmp_path)

        def _input_should_not_be_called(_prompt=""):
            raise AssertionError("input() called when stdin is not a TTY")

        monkeypatch.setattr("builtins.input", _input_should_not_be_called)
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)

        code = main([str(tmp_path), "--dry-run", "--no-banner", "--yes", "--prompt-unknown"])
        assert code == 0


class TestErrorHandling:
    def test_nonexistent_path(self, tmp_path: Path):
        code = main([str(tmp_path / "does-not-exist"), "--dry-run", "--no-banner", "--yes"])
        assert code == 2

    def test_empty_directory(self, tmp_path: Path, capsys):
        code = main([str(tmp_path), "--dry-run", "--no-banner", "--yes"])
        assert code == 0
        out = capsys.readouterr().out
        assert "No concert folders found" in out or "no concert" in out.lower()
