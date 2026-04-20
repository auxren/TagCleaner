"""Unit tests for tagcleaner.history — skip logic, persistence, surrogate safety."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tagcleaner.history import (
    History,
    HistoryEntry,
    TaggingOutcome,
    can_skip_by_mtime,
    load_history,
    save_history,
    should_skip,
)
from tagcleaner.models import Concert, SourceInfo, Track
from tagcleaner.tagger import Mode


def _entry(
    *,
    fp: str = "fp-abc",
    mtime: float | None = 1234567.0,
    outcome: TaggingOutcome | None = None,
) -> HistoryEntry:
    return HistoryEntry(
        folder="/some/folder",
        scanned_at="2026-01-01T00:00:00Z",
        fingerprint=fp,
        concert={"folder": "/some/folder"},
        tagging=outcome,
        folder_mtime=mtime,
    )


class TestShouldSkip:
    def test_no_entry_does_not_skip(self):
        assert should_skip(None, "any", Mode.IN_PLACE, None) is False

    def test_no_tagging_does_not_skip(self):
        assert should_skip(_entry(outcome=None), "fp-abc", Mode.IN_PLACE, None) is False

    def test_fingerprint_mismatch_does_not_skip(self):
        entry = _entry(outcome=TaggingOutcome(mode=Mode.IN_PLACE.value, applied_at="t", applied=3))
        assert should_skip(entry, "different", Mode.IN_PLACE, None) is False

    def test_prior_failure_does_not_skip(self):
        entry = _entry(outcome=TaggingOutcome(mode=Mode.IN_PLACE.value, applied_at="t", failed=1))
        assert should_skip(entry, "fp-abc", Mode.IN_PLACE, None) is False

    def test_in_place_skips_after_in_place(self):
        entry = _entry(outcome=TaggingOutcome(mode=Mode.IN_PLACE.value, applied_at="t", applied=2))
        assert should_skip(entry, "fp-abc", Mode.IN_PLACE, None) is True

    def test_in_place_does_not_skip_after_copy_to(self):
        entry = _entry(outcome=TaggingOutcome(mode=Mode.COPY_TO.value, applied_at="t", applied=2, copy_to="/dst"))
        assert should_skip(entry, "fp-abc", Mode.IN_PLACE, None) is False

    def test_dry_run_skips_after_any_real_run(self):
        entry = _entry(outcome=TaggingOutcome(mode=Mode.IN_PLACE.value, applied_at="t", applied=1))
        assert should_skip(entry, "fp-abc", Mode.DRY_RUN, None) is True
        entry2 = _entry(outcome=TaggingOutcome(mode=Mode.COPY_TO.value, applied_at="t", applied=1, copy_to="/d"))
        assert should_skip(entry2, "fp-abc", Mode.DRY_RUN, Path("/d")) is True

    def test_dry_run_does_not_skip_after_dry_run(self):
        entry = _entry(outcome=TaggingOutcome(mode=Mode.DRY_RUN.value, applied_at="t"))
        assert should_skip(entry, "fp-abc", Mode.DRY_RUN, None) is False

    def test_copy_to_destination_change_reprocesses(self, tmp_path: Path):
        dst_old = tmp_path / "old"
        dst_new = tmp_path / "new"
        dst_old.mkdir()
        dst_new.mkdir()
        entry = _entry(outcome=TaggingOutcome(
            mode=Mode.COPY_TO.value, applied_at="t", applied=2, copy_to=str(dst_old.resolve()),
        ))
        assert should_skip(entry, "fp-abc", Mode.COPY_TO, dst_new) is False
        assert should_skip(entry, "fp-abc", Mode.COPY_TO, dst_old) is True


class TestCanSkipByMtime:
    def test_requires_stored_mtime(self):
        entry = _entry(mtime=None, outcome=TaggingOutcome(mode=Mode.IN_PLACE.value, applied_at="t", applied=1))
        assert can_skip_by_mtime(entry, 1234567.0, Mode.IN_PLACE, None) is False

    def test_skips_when_mtime_matches_and_tagging_covers_mode(self):
        entry = _entry(mtime=1234567.0, outcome=TaggingOutcome(
            mode=Mode.IN_PLACE.value, applied_at="t", applied=3,
        ))
        assert can_skip_by_mtime(entry, 1234567.0, Mode.IN_PLACE, None) is True

    def test_does_not_skip_on_mtime_drift(self):
        entry = _entry(mtime=1234567.0, outcome=TaggingOutcome(
            mode=Mode.IN_PLACE.value, applied_at="t", applied=3,
        ))
        assert can_skip_by_mtime(entry, 1234568.0, Mode.IN_PLACE, None) is False

    def test_respects_prior_failure(self):
        entry = _entry(mtime=1234567.0, outcome=TaggingOutcome(
            mode=Mode.IN_PLACE.value, applied_at="t", failed=1,
        ))
        assert can_skip_by_mtime(entry, 1234567.0, Mode.IN_PLACE, None) is False


class TestPersistence:
    def _make_concert(self, folder: Path) -> Concert:
        return Concert(
            folder=folder,
            artist="Grateful Dead",
            date="1987-08-22",
            venue="Calaveras County Fairgrounds",
            city="Angels Camp",
            region="CA",
            source=SourceInfo(kind="SBD", mics=["Schoeps MK4"]),
            tracks=[Track(number=1, title="Touch of Grey")],
            audio_files=[folder / "01.flac"],
        )

    def test_record_and_reload(self, tmp_path: Path):
        history = History()
        folder = tmp_path / "show"
        folder.mkdir()
        concert = self._make_concert(folder)
        history.record_scan(concert, "fingerprint-xyz", 9876.0)
        history.record_tagging(folder, TaggingOutcome(
            mode=Mode.IN_PLACE.value, applied_at="2026-01-01T00:00:00Z", applied=1,
        ))
        save_path = tmp_path / "tagcleaner-history.json"
        save_history(history, save_path)

        reloaded = load_history(save_path)
        assert len(reloaded.entries) == 1
        entry = reloaded.get(folder)
        assert entry is not None
        assert entry.fingerprint == "fingerprint-xyz"
        assert entry.folder_mtime == 9876.0
        assert entry.tagging is not None
        assert entry.tagging.applied == 1
        assert entry.concert["artist"] == "Grateful Dead"

    def test_missing_file_returns_empty_history(self, tmp_path: Path):
        history = load_history(tmp_path / "no-such-file.json")
        assert history.entries == {}

    def test_unknown_schema_returns_empty(self, tmp_path: Path):
        path = tmp_path / "h.json"
        path.write_text(json.dumps({"schema": 999, "entries": {}}), encoding="utf-8")
        assert load_history(path).entries == {}

    def test_corrupt_file_returns_empty(self, tmp_path: Path):
        path = tmp_path / "h.json"
        path.write_text("{not json", encoding="utf-8")
        assert load_history(path).entries == {}

    def test_survives_surrogate_filenames(self, tmp_path: Path):
        """Linux filenames with non-UTF-8 bytes surface as lone surrogates.
        save_history must not blow up on encode."""
        history = History()
        concert = Concert(
            folder=tmp_path / "show",
            artist="Weird\udc8aArtist",
            date="2000-01-01",
            audio_files=[tmp_path / "show" / "bad\udc8a.flac"],
            tracks=[Track(number=1, title="Glitch\udc8aTrack")],
        )
        history.record_scan(concert, "fp", 1.0)
        path = tmp_path / "h.json"
        save_history(history, path)  # must not raise
        reloaded = load_history(path)
        assert len(reloaded.entries) == 1

    def test_record_scan_preserves_prior_tagging(self, tmp_path: Path):
        history = History()
        folder = tmp_path / "show"
        folder.mkdir()
        concert = self._make_concert(folder)
        history.record_scan(concert, "fp1", 1.0)
        history.record_tagging(folder, TaggingOutcome(
            mode=Mode.IN_PLACE.value, applied_at="t", applied=1,
        ))
        # Re-scan (fingerprint may change) — tagging outcome should persist.
        history.record_scan(concert, "fp2", 2.0)
        entry = history.get(folder)
        assert entry is not None
        assert entry.tagging is not None
        assert entry.fingerprint == "fp2"
        assert entry.folder_mtime == 2.0
