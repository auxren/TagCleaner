"""Tests for tagcleaner.dedupe.

The Chromaprint similarity math is tested against synthetic int sequences
so we don't need ``fpcalc`` installed in CI. The folder-clustering and
keeper-selection logic is tested with stub ``FolderFingerprint`` objects.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from tagcleaner.dedupe import (
    DEFAULT_FOLDER_THRESHOLD,
    DEFAULT_FP_THRESHOLD,
    FingerprintCache,
    FolderFingerprint,
    _bit_similarity,
    are_audio_duplicates,
    cluster_duplicates,
    folders_match,
    pick_keeper,
)


class TestBitSimilarity:
    def test_identical_sequences_return_one(self):
        a = [0xDEADBEEF, 0xCAFEBABE, 0x12345678]
        assert _bit_similarity(a, a) == pytest.approx(1.0)

    def test_completely_different_returns_zero(self):
        a = [0x00000000, 0x00000000]
        b = [0xFFFFFFFF, 0xFFFFFFFF]
        assert _bit_similarity(a, b) == pytest.approx(0.0)

    def test_half_different_returns_half(self):
        a = [0x00000000]
        b = [0x0000FFFF]  # 16 bits flipped of 32
        assert _bit_similarity(a, b) == pytest.approx(0.5)

    def test_truncates_to_shorter(self):
        a = [0xAAAAAAAA, 0xAAAAAAAA, 0xAAAAAAAA]
        b = [0xAAAAAAAA]
        assert _bit_similarity(a, b) == pytest.approx(1.0)

    def test_empty_returns_zero(self):
        assert _bit_similarity([], [1, 2, 3]) == 0.0
        assert _bit_similarity([1, 2, 3], []) == 0.0


def _make_folder_fp(name: str, tracks: list[tuple[float, str]], tmp_path: Path) -> FolderFingerprint:
    folder = tmp_path / name
    folder.mkdir(exist_ok=True)
    return FolderFingerprint(
        folder=folder,
        tracks=[(folder / f"{i:02d}.flac", dur, fp) for i, (dur, fp) in enumerate(tracks)],
    )


class TestFoldersMatch:
    """folders_match patches compare_fingerprints to avoid fpcalc."""

    def test_identical_folders_match(self, tmp_path):
        a = _make_folder_fp("a", [(180.0, "fpA"), (200.0, "fpB"), (175.0, "fpC")], tmp_path)
        b = _make_folder_fp("b", [(180.0, "fpA"), (200.0, "fpB"), (175.0, "fpC")], tmp_path)
        with patch("tagcleaner.dedupe.compare_fingerprints", return_value=1.0):
            is_dupe, frac = folders_match(a, b)
        assert is_dupe is True
        assert frac == pytest.approx(1.0)

    def test_unrelated_folders_don_t_match(self, tmp_path):
        a = _make_folder_fp("a", [(180.0, "fpA"), (200.0, "fpB")], tmp_path)
        b = _make_folder_fp("b", [(180.0, "fpX"), (200.0, "fpY")], tmp_path)
        with patch("tagcleaner.dedupe.compare_fingerprints", return_value=0.2):
            is_dupe, frac = folders_match(a, b)
        assert is_dupe is False
        assert frac == 0.0

    def test_duration_mismatch_disqualifies_a_pair(self, tmp_path):
        a = _make_folder_fp("a", [(100.0, "fpA"), (200.0, "fpB")], tmp_path)
        b = _make_folder_fp("b", [(180.0, "fpA"), (200.0, "fpB")], tmp_path)  # first off by 80s
        with patch("tagcleaner.dedupe.compare_fingerprints", return_value=1.0):
            is_dupe, frac = folders_match(a, b, folder_threshold=0.5, duration_tolerance=7.0)
        # 1 of 2 paired matches → 0.5; threshold 0.5 → match.
        assert is_dupe is True
        assert frac == pytest.approx(0.5)

    def test_subset_does_not_claim_superset(self, tmp_path):
        """A 10-track folder vs a 3-track folder: 3 of 10 = 30%, below default 80%."""
        long = _make_folder_fp("long", [(180.0, f"fp{i}") for i in range(10)], tmp_path)
        short = _make_folder_fp("short", [(180.0, "fp0"), (180.0, "fp1"), (180.0, "fp2")], tmp_path)
        with patch("tagcleaner.dedupe.compare_fingerprints", return_value=1.0):
            is_dupe, frac = folders_match(long, short)
        assert is_dupe is False
        assert frac == pytest.approx(0.3)

    def test_empty_folder_never_matches(self, tmp_path):
        a = _make_folder_fp("a", [(180.0, "fpA")], tmp_path)
        empty = _make_folder_fp("empty", [], tmp_path)
        with patch("tagcleaner.dedupe.compare_fingerprints", return_value=1.0):
            is_dupe, _ = folders_match(a, empty)
        assert is_dupe is False


class TestClusterDuplicates:
    def test_three_identical_folders_form_one_cluster(self, tmp_path):
        folders = [
            _make_folder_fp(f"f{i}", [(180.0, "fpA"), (200.0, "fpB")], tmp_path)
            for i in range(3)
        ]
        with patch("tagcleaner.dedupe.compare_fingerprints", return_value=1.0):
            clusters = cluster_duplicates(folders)
        assert len(clusters) == 1
        assert len(clusters[0]) == 3

    def test_two_unrelated_pairs_form_two_clusters(self, tmp_path):
        # a == b (similarity 1.0); c == d (similarity 1.0); but a vs c: 0.0
        a = _make_folder_fp("a", [(180.0, "X1"), (200.0, "X2")], tmp_path)
        b = _make_folder_fp("b", [(180.0, "X1"), (200.0, "X2")], tmp_path)
        c = _make_folder_fp("c", [(180.0, "Y1"), (200.0, "Y2")], tmp_path)
        d = _make_folder_fp("d", [(180.0, "Y1"), (200.0, "Y2")], tmp_path)
        sim_table = {("X1", "X1"): 1.0, ("X2", "X2"): 1.0,
                     ("Y1", "Y1"): 1.0, ("Y2", "Y2"): 1.0}
        def fake_cmp(x, y): return sim_table.get((x, y), 0.0)
        with patch("tagcleaner.dedupe.compare_fingerprints", side_effect=fake_cmp):
            clusters = cluster_duplicates([a, b, c, d])
        assert len(clusters) == 2
        sizes = sorted(len(c) for c in clusters)
        assert sizes == [2, 2]

    def test_singletons_are_dropped(self, tmp_path):
        a = _make_folder_fp("a", [(180.0, "X")], tmp_path)
        b = _make_folder_fp("b", [(180.0, "Y")], tmp_path)
        with patch("tagcleaner.dedupe.compare_fingerprints", return_value=0.0):
            assert cluster_duplicates([a, b]) == []


class TestPickKeeper:
    def test_largest_picks_biggest_total_size(self, tmp_path):
        f1 = tmp_path / "f1"; f1.mkdir()
        f2 = tmp_path / "f2"; f2.mkdir()
        big = f1 / "big.flac"; big.write_bytes(b"x" * 1000)
        small = f2 / "small.flac"; small.write_bytes(b"x" * 10)
        a = FolderFingerprint(folder=f1, tracks=[(big, 180.0, "fp")])
        b = FolderFingerprint(folder=f2, tracks=[(small, 180.0, "fp")])
        assert pick_keeper([a, b], strategy="largest") is a

    def test_most_tracks_picks_longer_list(self, tmp_path):
        f1 = tmp_path / "f1"; f1.mkdir()
        f2 = tmp_path / "f2"; f2.mkdir()
        a = FolderFingerprint(folder=f1, tracks=[(f1 / "01.flac", 180.0, "fp")] * 2)
        b = FolderFingerprint(folder=f2, tracks=[(f2 / "01.flac", 180.0, "fp")] * 5)
        assert pick_keeper([a, b], strategy="most-tracks") is b

    def test_unknown_strategy_raises(self, tmp_path):
        a = FolderFingerprint(folder=tmp_path, tracks=[])
        with pytest.raises(ValueError):
            pick_keeper([a], strategy="bogus")


class TestFingerprintFile:
    """fingerprint_file should prefer pyacoustid when available, fall back
    to a direct fpcalc subprocess otherwise."""

    def test_uses_pyacoustid_when_available(self, tmp_path):
        from tagcleaner import dedupe
        fake_path = tmp_path / "x.flac"
        fake_path.touch()

        class _FakeAcoustid:
            FingerprintGenerationError = Exception
            @staticmethod
            def fingerprint_file(p, maxlength):
                return 180.0, b"FPSTR"

        with patch("tagcleaner.dedupe._try_import_acoustid",
                   return_value=_FakeAcoustid):
            result = dedupe.fingerprint_file(fake_path)
        assert result == (180.0, "FPSTR")

    def test_falls_back_to_fpcalc_subprocess(self, tmp_path):
        from tagcleaner import dedupe
        fake_path = tmp_path / "x.flac"
        fake_path.touch()
        import subprocess
        fake_result = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout='{"duration": 250.5, "fingerprint": "ABC123"}',
            stderr="",
        )
        with patch("tagcleaner.dedupe._try_import_acoustid", return_value=None), \
             patch("subprocess.run", return_value=fake_result):
            result = dedupe.fingerprint_file(fake_path)
        assert result == (250.5, "ABC123")

    def test_returns_none_when_neither_works(self, tmp_path):
        from tagcleaner import dedupe
        with patch("tagcleaner.dedupe._try_import_acoustid", return_value=None), \
             patch("subprocess.run", side_effect=FileNotFoundError):
            result = dedupe.fingerprint_file(tmp_path / "x.flac")
        assert result is None


class TestAreAudioDuplicates:
    """High-level helper used by server-side collision handlers."""

    def test_returns_false_for_folders_with_no_audio(self, tmp_path):
        # Both folders empty — no fingerprints, no possibility of match.
        a = tmp_path / "a"; a.mkdir()
        b = tmp_path / "b"; b.mkdir()
        is_dupe, frac = are_audio_duplicates(a, b)
        assert is_dupe is False
        assert frac == 0.0

    def test_calls_through_to_fingerprint_folder_and_match(self, tmp_path):
        # Stub fingerprint_folder + compare_fingerprints to validate plumbing
        # without running fpcalc.
        a = tmp_path / "a"; a.mkdir()
        b = tmp_path / "b"; b.mkdir()
        fp_a = FolderFingerprint(folder=a, tracks=[(a / "01.flac", 180.0, "X")])
        fp_b = FolderFingerprint(folder=b, tracks=[(b / "01.flac", 180.0, "X")])
        with patch("tagcleaner.dedupe.fingerprint_folder",
                   side_effect=[fp_a, fp_b]), \
             patch("tagcleaner.dedupe.compare_fingerprints", return_value=1.0):
            is_dupe, frac = are_audio_duplicates(a, b)
        assert is_dupe is True
        assert frac == pytest.approx(1.0)

    def test_passes_thresholds_through(self, tmp_path):
        a = tmp_path / "a"; a.mkdir()
        b = tmp_path / "b"; b.mkdir()
        fp_a = FolderFingerprint(folder=a, tracks=[(a / "01.flac", 180.0, "X")])
        fp_b = FolderFingerprint(folder=b, tracks=[(b / "01.flac", 180.0, "X")])
        # Pass a fp_threshold of 1.0001 — impossible — and ensure it returns False.
        with patch("tagcleaner.dedupe.fingerprint_folder",
                   side_effect=[fp_a, fp_b]), \
             patch("tagcleaner.dedupe.compare_fingerprints", return_value=1.0):
            is_dupe, _ = are_audio_duplicates(a, b, fp_threshold=1.0001)
        assert is_dupe is False


class TestFingerprintCache:
    def test_load_missing_file_returns_empty(self, tmp_path):
        cache = FingerprintCache.load(tmp_path / "nope.json")
        assert cache.entries == {}
        assert not cache.dirty

    def test_load_returns_existing_entries(self, tmp_path):
        cache_path = tmp_path / "cache.json"
        cache_path.write_text(json.dumps({"foo": {"mtime": 1.0, "size": 10,
                                                  "duration": 180.0, "fingerprint": "AAA"}}))
        cache = FingerprintCache.load(cache_path)
        assert "foo" in cache.entries

    def test_save_writes_dirty_cache(self, tmp_path):
        cache_path = tmp_path / "cache.json"
        cache = FingerprintCache(path=cache_path, entries={"x": {"a": 1}}, dirty=True)
        cache.save()
        assert json.loads(cache_path.read_text()) == {"x": {"a": 1}}
        assert not cache.dirty

    def test_save_skips_clean_cache(self, tmp_path):
        cache_path = tmp_path / "cache.json"
        cache = FingerprintCache(path=cache_path, entries={}, dirty=False)
        cache.save()
        assert not cache_path.exists()

    def test_get_returns_cached_when_stat_matches(self, tmp_path):
        f = tmp_path / "song.flac"
        f.write_bytes(b"abc")
        stat = f.stat()
        cache = FingerprintCache(path=None, entries={
            str(f): {"mtime": stat.st_mtime, "size": stat.st_size,
                     "duration": 100.0, "fingerprint": "CACHED"}
        })
        # If cached hit, fingerprint_file is never called.
        with patch("tagcleaner.dedupe.fingerprint_file", side_effect=AssertionError("must not call")):
            result = cache.get_or_compute(f)
        assert result == (100.0, "CACHED")

    def test_get_recomputes_when_stat_changes(self, tmp_path):
        f = tmp_path / "song.flac"
        f.write_bytes(b"abc")
        cache = FingerprintCache(path=None, entries={
            str(f): {"mtime": 0.0, "size": 999, "duration": 100.0, "fingerprint": "STALE"}
        })
        with patch("tagcleaner.dedupe.fingerprint_file", return_value=(180.0, "FRESH")):
            result = cache.get_or_compute(f)
        assert result == (180.0, "FRESH")
        assert cache.entries[str(f)]["fingerprint"] == "FRESH"
        assert cache.dirty
