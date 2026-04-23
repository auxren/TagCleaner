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


class TestApplyPlansWAV:
    """WAV files carry ID3 tags in a chunk inside the RIFF container; mutagen
    reads/writes them through ``mutagen.wave.WAVE``. Tests confirm the same
    ARTIST/ALBUM/TRACKNUMBER pipeline works as for FLAC and MP3."""

    def test_writes_id3_to_wav(self, tmp_path: Path, make_wav):
        from mutagen.wave import WAVE
        folder = tmp_path / "show"
        audio = make_wav(folder / "01.wav")
        tracks = [Track(number=1, title="WAV Track")]
        plans = build_plans(_concert(folder, [audio], tracks))
        results = apply_plans(plans, Mode.IN_PLACE)
        assert all(r.ok for r in results), [r.error for r in results]

        tags = WAVE(str(audio)).tags
        assert str(tags["TPE1"]) == "Test Artist"
        assert str(tags["TPE2"]) == "Test Artist"
        assert str(tags["TIT2"]) == "WAV Track"
        assert str(tags["TRCK"]) == "01"

    def test_uppercase_extension_handled(self, tmp_path: Path, make_wav):
        from mutagen.wave import WAVE
        folder = tmp_path / "show"
        audio = make_wav(folder / "01.WAV")
        plans = build_plans(_concert(folder, [audio], [Track(number=1, title="A")]))
        results = apply_plans(plans, Mode.IN_PLACE)
        assert all(r.ok for r in results), [r.error for r in results]
        tags = WAVE(str(audio)).tags
        assert str(tags["TPE1"]) == "Test Artist"

    def test_already_tagged_only_rewrites_album(self, tmp_path: Path, make_wav):
        from mutagen.wave import WAVE
        from mutagen.id3 import TPE1, TPE2, TALB, TIT2, TRCK
        folder = tmp_path / "show"
        audio = make_wav(folder / "01.wav")
        # Pre-tag the file with everything *except* the canonical album.
        pre = WAVE(str(audio))
        pre.add_tags()
        pre.tags["TPE1"] = TPE1(encoding=3, text="Test Artist")
        pre.tags["TPE2"] = TPE2(encoding=3, text="Test Artist")
        pre.tags["TALB"] = TALB(encoding=3, text="Old Album")
        pre.tags["TIT2"] = TIT2(encoding=3, text="A")
        pre.tags["TRCK"] = TRCK(encoding=3, text="01")
        pre.save()

        plans = build_plans(_concert(folder, [audio], [Track(number=1, title="A")]))
        results = apply_plans(plans, Mode.IN_PLACE)
        assert all(r.ok for r in results), [r.error for r in results]
        # The plan-built album wins; everything else stays (album-only update).
        tags = WAVE(str(audio)).tags
        assert str(tags["TALB"]) != "Old Album"
        assert str(tags["TPE1"]) == "Test Artist"  # untouched


class TestApplyPlansError:
    def test_missing_file_is_reported(self, tmp_path: Path, make_flac):
        folder = tmp_path / "show"
        audio = make_flac(folder / "01.flac")
        plan = build_plans(_concert(folder, [audio], [Track(number=1, title="A")]))[0]
        audio.unlink()  # remove before apply
        results = apply_plans([plan], Mode.IN_PLACE)
        assert results[0].ok is False
        assert results[0].error


class TestAlreadyTaggedSkip:
    """Files that already have every required field get only ALBUM rewritten
    (or no write at all when ALBUM already matches)."""

    def test_fully_tagged_flac_rewrites_only_album(
        self, tmp_path: Path, make_flac,
    ):
        folder = tmp_path / "show"
        audio = make_flac(folder / "01.flac")
        pre = FLAC(str(audio))
        pre["ARTIST"] = "Hand Tagged Artist"
        pre["TITLE"] = "Hand Tagged Title"
        pre["TRACKNUMBER"] = "05"
        pre["DATE"] = "1999-12-31"
        pre["ALBUM"] = "Old Album Format"
        pre.save()

        tracks = [Track(number=1, title="Parser Title")]
        plans = build_plans(_concert(folder, [audio], tracks))
        results = apply_plans(plans, Mode.IN_PLACE)

        assert all(r.ok for r in results)
        assert results[0].album_only is True
        assert results[0].changed is True

        f = FLAC(str(audio))
        # Only ALBUM was updated; everything else preserved.
        assert f["ARTIST"] == ["Hand Tagged Artist"]
        assert f["TITLE"] == ["Hand Tagged Title"]
        assert f["TRACKNUMBER"] == ["05"]
        assert f["DATE"] == ["1999-12-31"]
        # Album now matches the canonical format the planner would emit.
        assert f["ALBUM"][0].startswith("2000-01-01 ")

    def test_matching_album_is_a_noop(self, tmp_path: Path, make_flac):
        folder = tmp_path / "show"
        audio = make_flac(folder / "01.flac")
        tracks = [Track(number=1, title="T")]
        c = _concert(folder, [audio], tracks)
        plans = build_plans(c)

        pre = FLAC(str(audio))
        pre["ARTIST"] = "Existing Artist"
        pre["TITLE"] = "Existing Title"
        pre["TRACKNUMBER"] = "05"
        pre["DATE"] = "1999-12-31"
        pre["ALBUM"] = plans[0].album
        pre.save()

        results = apply_plans(plans, Mode.IN_PLACE)
        assert results[0].ok
        assert results[0].changed is False
        assert results[0].album_only is True

    def test_partial_tags_trigger_full_write(self, tmp_path: Path, make_flac):
        folder = tmp_path / "show"
        audio = make_flac(folder / "01.flac")
        pre = FLAC(str(audio))
        pre["ARTIST"] = "Existing Artist"
        # TITLE deliberately missing — a gap triggers full plan.
        pre["TRACKNUMBER"] = "05"
        pre.save()

        tracks = [Track(number=1, title="Parser Title")]
        plans = build_plans(_concert(folder, [audio], tracks))
        results = apply_plans(plans, Mode.IN_PLACE)

        assert all(r.ok for r in results)
        assert results[0].album_only is False

        f = FLAC(str(audio))
        # Full plan applied — ARTIST gets overwritten to the parsed value.
        assert f["ARTIST"] == ["Test Artist"]
        assert f["TITLE"] == ["Parser Title"]
        assert f["TRACKNUMBER"] == ["01"]

    def test_fully_tagged_mp3_rewrites_only_album(
        self, tmp_path: Path, make_mp3,
    ):
        folder = tmp_path / "show"
        audio = make_mp3(folder / "01.mp3")
        pre = EasyID3(str(audio))
        pre["artist"] = "Hand Tagged"
        pre["title"] = "Hand Title"
        pre["tracknumber"] = "07"
        pre["date"] = "1999"
        pre["album"] = "Old"
        pre.save()

        tracks = [Track(number=1, title="Parser Title")]
        plans = build_plans(_concert(folder, [audio], tracks))
        results = apply_plans(plans, Mode.IN_PLACE)

        assert all(r.ok for r in results)
        assert results[0].album_only is True

        tags = EasyID3(str(audio))
        assert tags["artist"] == ["Hand Tagged"]
        assert tags["title"] == ["Hand Title"]
        assert tags["album"][0].startswith("2000-01-01 ")

    def test_metadata_only_plan_also_honours_skip(
        self, tmp_path: Path, make_flac,
    ):
        # Metadata-only plans have no title/tracknumber to check; only ARTIST
        # (and DATE, if the plan has one) need to be present.
        folder = tmp_path / "show"
        audio = make_flac(folder / "01.flac")
        pre = FLAC(str(audio))
        pre["ARTIST"] = "Hand Tagged"
        pre["DATE"] = "1999-12-31"
        pre["TITLE"] = "Preserved"
        pre["TRACKNUMBER"] = "42"
        pre["ALBUM"] = "stale"
        pre.save()

        c = _concert(folder, [audio], tracks=[])
        plans = build_plans(c, metadata_only=True)
        results = apply_plans(plans, Mode.IN_PLACE)

        assert all(r.ok for r in results)
        assert results[0].album_only is True
        f = FLAC(str(audio))
        assert f["ARTIST"] == ["Hand Tagged"]
        assert f["TITLE"] == ["Preserved"]
        assert f["TRACKNUMBER"] == ["42"]


class TestMinimalTags:
    """--minimal-tags writes only ARTIST/ALBUMARTIST/ALBUM/TRACKNUMBER and
    leaves any existing DATE/TITLE/DISC tags alone."""

    def test_flac_writes_only_core_fields(self, tmp_path: Path, make_flac):
        folder = tmp_path / "show"
        audio = make_flac(folder / "01.flac")
        tracks = [Track(number=1, title="Parser Title", disc=1, disc_total=2)]
        plans = build_plans(_concert(folder, [audio], tracks), minimal=True)
        results = apply_plans(plans, Mode.IN_PLACE)

        assert all(r.ok for r in results)
        f = FLAC(str(audio))
        assert f["ARTIST"] == ["Test Artist"]
        assert f["ALBUMARTIST"] == ["Test Artist"]
        assert f["TRACKNUMBER"] == ["01"]
        assert f["ALBUM"][0].startswith("2000-01-01 ")
        assert "DATE" not in f
        assert "TITLE" not in f
        assert "DISCNUMBER" not in f

    def test_flac_preserves_existing_title_and_date(
        self, tmp_path: Path, make_flac,
    ):
        folder = tmp_path / "show"
        audio = make_flac(folder / "01.flac")
        pre = FLAC(str(audio))
        pre["TITLE"] = "Existing Title"
        pre["DATE"] = "1999-12-31"
        pre["DISCNUMBER"] = "3"
        pre.save()

        tracks = [Track(number=1, title="Parser Title")]
        plans = build_plans(_concert(folder, [audio], tracks), minimal=True)
        results = apply_plans(plans, Mode.IN_PLACE)

        assert all(r.ok for r in results)
        f = FLAC(str(audio))
        # Core fields rewritten.
        assert f["ARTIST"] == ["Test Artist"]
        assert f["TRACKNUMBER"] == ["01"]
        # Non-core fields preserved verbatim.
        assert f["TITLE"] == ["Existing Title"]
        assert f["DATE"] == ["1999-12-31"]
        assert f["DISCNUMBER"] == ["3"]

    def test_mp3_minimal(self, tmp_path: Path, make_mp3):
        folder = tmp_path / "show"
        audio = make_mp3(folder / "01.mp3")
        pre = EasyID3(str(audio))
        pre["title"] = "Keep Me"
        pre["date"] = "1999"
        pre.save()

        tracks = [Track(number=1, title="Parser Title")]
        plans = build_plans(_concert(folder, [audio], tracks), minimal=True)
        results = apply_plans(plans, Mode.IN_PLACE)

        assert all(r.ok for r in results)
        tags = EasyID3(str(audio))
        assert tags["artist"] == ["Test Artist"]
        assert tags["tracknumber"] == ["01"]
        assert tags["title"] == ["Keep Me"]
        assert tags["date"] == ["1999"]

    def test_already_core_tagged_is_album_only(
        self, tmp_path: Path, make_flac,
    ):
        # In minimal mode only ARTIST + TRACKNUMBER need to be present for
        # the "already tagged, just rewrite ALBUM" fast path to fire — DATE
        # and TITLE don't count.
        folder = tmp_path / "show"
        audio = make_flac(folder / "01.flac")
        pre = FLAC(str(audio))
        pre["ARTIST"] = "Hand Tagged"
        pre["TRACKNUMBER"] = "07"
        pre["ALBUM"] = "Stale"
        pre.save()

        tracks = [Track(number=1, title="Parser Title")]
        plans = build_plans(_concert(folder, [audio], tracks), minimal=True)
        results = apply_plans(plans, Mode.IN_PLACE)

        assert results[0].album_only is True
        assert results[0].changed is True
        f = FLAC(str(audio))
        assert f["ARTIST"] == ["Hand Tagged"]  # not rewritten
        assert f["ALBUM"][0].startswith("2000-01-01 ")
