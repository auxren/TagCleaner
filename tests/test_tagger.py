"""Unit tests for tagcleaner.tagger — plan building and tag writing."""
from __future__ import annotations

from pathlib import Path

from mutagen.easyid3 import EasyID3
from mutagen.flac import FLAC

from tagcleaner.models import Concert, SourceInfo, Track
from tagcleaner.tagger import Mode, apply_plans, build_plans


def _concert(folder: Path, audio: list[Path], tracks: list[Track]) -> Concert:
    return Concert(
        folder=folder,
        artist="Test Artist",
        date="2000-01-01",
        venue="Test Venue",
        city="Test City",
        region="CA",
        source=SourceInfo(kind="SBD"),
        tracks=tracks,
        audio_files=audio,
    )


class TestBuildPlans:
    def test_in_place_targets_original_paths(self, tmp_path: Path, make_flac):
        folder = tmp_path / "show"
        audio = [make_flac(folder / "01.flac"), make_flac(folder / "02.flac")]
        tracks = [Track(number=1, title="A"), Track(number=2, title="B")]
        c = _concert(folder, audio, tracks)
        plans = build_plans(c)
        assert len(plans) == 2
        assert plans[0].file == audio[0]
        assert plans[0].dest == audio[0]
        assert plans[0].title == "A"
        assert plans[0].track == 1

    def test_copy_to_mirrors_tree(self, tmp_path: Path, make_flac):
        src_root = tmp_path / "src"
        folder = src_root / "show"
        audio = [make_flac(folder / "01.flac")]
        tracks = [Track(number=1, title="A")]
        c = _concert(folder, audio, tracks)
        dest_root = tmp_path / "dst"

        plans = build_plans(c, copy_to_root=dest_root, source_root=src_root)
        assert plans[0].file == audio[0]
        assert plans[0].dest == dest_root / "show" / "01.flac"

    def test_empty_when_no_tracks(self, tmp_path: Path, make_flac):
        folder = tmp_path / "show"
        audio = [make_flac(folder / "01.flac")]
        c = _concert(folder, audio, tracks=[])
        assert build_plans(c) == []

    def test_pairs_up_to_shorter_list(self, tmp_path: Path, make_flac):
        folder = tmp_path / "show"
        audio = [make_flac(folder / f"0{i}.flac") for i in (1, 2, 3)]
        tracks = [Track(number=1, title="A"), Track(number=2, title="B")]
        c = _concert(folder, audio, tracks)
        plans = build_plans(c)
        assert len(plans) == 2

    def test_metadata_only_emits_one_plan_per_audio(self, tmp_path: Path, make_flac):
        folder = tmp_path / "show"
        audio = [make_flac(folder / f"0{i}.flac") for i in (1, 2, 3)]
        # Intentional mismatch: only one track parsed, three audio files.
        tracks = [Track(number=1, title="Only One")]
        c = _concert(folder, audio, tracks)
        plans = build_plans(c, metadata_only=True)
        assert len(plans) == 3
        assert all(p.track is None and p.title is None for p in plans)
        assert all(p.artist == "Test Artist" for p in plans)

    def test_metadata_only_works_with_no_tracks(self, tmp_path: Path, make_flac):
        folder = tmp_path / "show"
        audio = [make_flac(folder / "01.flac")]
        c = _concert(folder, audio, tracks=[])
        plans = build_plans(c, metadata_only=True)
        assert len(plans) == 1
        assert plans[0].title is None
        assert plans[0].track is None


class TestApplyPlansDryRun:
    def test_dry_run_leaves_file_untouched(self, tmp_path: Path, make_flac):
        folder = tmp_path / "show"
        audio = make_flac(folder / "01.flac")
        plans = build_plans(_concert(folder, [audio], [Track(number=1, title="A")]))
        results = apply_plans(plans, Mode.DRY_RUN)
        assert all(r.ok for r in results)
        # Tags must not have been written.
        f = FLAC(str(audio))
        assert "ARTIST" not in f


class TestApplyPlansInPlaceFLAC:
    def test_writes_vorbis_tags(self, tmp_path: Path, make_flac):
        folder = tmp_path / "show"
        audio = make_flac(folder / "01.flac")
        tracks = [Track(number=1, title="Track One")]
        plans = build_plans(_concert(folder, [audio], tracks))
        results = apply_plans(plans, Mode.IN_PLACE)
        assert all(r.ok for r in results), [r.error for r in results]

        f = FLAC(str(audio))
        assert f["ARTIST"] == ["Test Artist"]
        assert f["ALBUMARTIST"] == ["Test Artist"]
        assert f["TITLE"] == ["Track One"]
        assert f["TRACKNUMBER"] == ["01"]
        assert f["DATE"] == ["2000-01-01"]
        assert "DISCNUMBER" not in f

    def test_multi_disc_writes_disc_tags(self, tmp_path: Path, make_flac):
        folder = tmp_path / "show"
        audio = [make_flac(folder / "01.flac"), make_flac(folder / "02.flac")]
        tracks = [
            Track(number=1, title="Set1-A", disc=1, disc_total=2),
            Track(number=1, title="Set2-A", disc=2, disc_total=2),
        ]
        plans = build_plans(_concert(folder, audio, tracks))
        results = apply_plans(plans, Mode.IN_PLACE)
        assert all(r.ok for r in results)
        f1 = FLAC(str(audio[0]))
        assert f1["DISCNUMBER"] == ["1"]
        assert f1["DISCTOTAL"] == ["2"]


class TestApplyPlansMetadataOnly:
    def test_preserves_existing_title_and_tracknumber(self, tmp_path: Path, make_flac):
        folder = tmp_path / "show"
        audio = make_flac(folder / "01.flac")
        # Pre-populate with per-track tags that MUST survive the metadata-only
        # write (the whole point: we don't have a reliable setlist to overwrite
        # them with, so leave the per-file work alone).
        pre = FLAC(str(audio))
        pre["TITLE"] = "Existing Title"
        pre["TRACKNUMBER"] = "07"
        pre.save()

        c = _concert(folder, [audio], tracks=[])
        plans = build_plans(c, metadata_only=True)
        results = apply_plans(plans, Mode.IN_PLACE)
        assert all(r.ok for r in results)

        f = FLAC(str(audio))
        # Concert-level metadata gets stamped.
        assert f["ARTIST"] == ["Test Artist"]
        assert f["ALBUMARTIST"] == ["Test Artist"]
        assert f["DATE"] == ["2000-01-01"]
        # Per-track fields untouched.
        assert f["TITLE"] == ["Existing Title"]
        assert f["TRACKNUMBER"] == ["07"]


class TestApplyPlansCopyTo:
    def test_copies_then_tags(self, tmp_path: Path, make_flac):
        src_root = tmp_path / "src"
        folder = src_root / "show"
        audio = make_flac(folder / "01.flac")
        dest_root = tmp_path / "dst"
        tracks = [Track(number=1, title="Copy Me")]
        plans = build_plans(_concert(folder, [audio], tracks), copy_to_root=dest_root, source_root=src_root)
        results = apply_plans(plans, Mode.COPY_TO)
        assert all(r.ok for r in results)

        dest = dest_root / "show" / "01.flac"
        assert dest.exists()
        # Source is untouched.
        src_tags = FLAC(str(audio))
        assert "ARTIST" not in src_tags
        # Copy is tagged.
        dst_tags = FLAC(str(dest))
        assert dst_tags["TITLE"] == ["Copy Me"]


class TestApplyPlansMP3:
    def test_writes_id3(self, tmp_path: Path, make_mp3):
        folder = tmp_path / "show"
        audio = make_mp3(folder / "01.mp3")
        tracks = [Track(number=1, title="MP3 Track")]
        plans = build_plans(_concert(folder, [audio], tracks))
        results = apply_plans(plans, Mode.IN_PLACE)
        assert all(r.ok for r in results), [r.error for r in results]

        tags = EasyID3(str(audio))
        assert tags["artist"] == ["Test Artist"]
        assert tags["title"] == ["MP3 Track"]
        assert tags["tracknumber"] == ["01"]


class TestApplyPlansError:
    def test_missing_file_is_reported(self, tmp_path: Path, make_flac):
        folder = tmp_path / "show"
        audio = make_flac(folder / "01.flac")
        plan = build_plans(_concert(folder, [audio], [Track(number=1, title="A")]))[0]
        audio.unlink()  # remove before apply
        results = apply_plans([plan], Mode.IN_PLACE)
        assert results[0].ok is False
        assert results[0].error
