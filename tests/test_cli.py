"""End-to-end CLI tests: dry-run, in-place, history skip, rescan-all."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from mutagen.flac import FLAC

from tagcleaner.cli import main


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


class TestErrorHandling:
    def test_nonexistent_path(self, tmp_path: Path):
        code = main([str(tmp_path / "does-not-exist"), "--dry-run", "--no-banner", "--yes"])
        assert code == 2

    def test_empty_directory(self, tmp_path: Path, capsys):
        code = main([str(tmp_path), "--dry-run", "--no-banner", "--yes"])
        assert code == 0
        out = capsys.readouterr().out
        assert "No concert folders found" in out or "no concert" in out.lower()
