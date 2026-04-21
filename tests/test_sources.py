"""Unit tests for tagcleaner.sources."""
from __future__ import annotations

import pytest

from tagcleaner.sources import detect_source


class TestKindDetection:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("gd1987-08-22.sbd.shnf", "SBD"),
            ("ph1997-12-31.aud", "AUD"),
            ("king crimson 1974 pre-fm broadcast", "Pre-FM"),
            ("show.matrix.shnf", "Matrix"),
            ("mtx mix", "Matrix"),
            ("radio broadcast capture", "FM"),
            ("audience recording", "AUD"),
            ("soundboard master", "SBD"),
        ],
    )
    def test_kind_identified(self, text: str, expected: str):
        assert detect_source(text).kind == expected

    def test_pre_fm_beats_fm(self):
        # Pre-FM regex must win over bare FM.
        src = detect_source("pre-fm soundboard")
        assert src.kind == "Pre-FM"

    def test_no_kind_returns_none(self):
        assert detect_source("just some text").kind is None


class TestMicDetection:
    def test_akg_specific(self):
        mics = detect_source("rig: AKG 414 matched pair").mics
        assert any("AKG 414" in m for m in mics)

    def test_schoeps_mk4(self):
        mics = detect_source("Schoeps MK4").mics
        assert any("Schoeps MK4" in m for m in mics)

    def test_bare_mk_number_expands_to_schoeps(self):
        mics = detect_source("MK4 pair").mics
        assert any("Schoeps MK4" in m for m in mics)

    def test_mic_family_fallback_dropped_when_specific_present(self):
        mics = detect_source("AKG C414 and an AKG 460").mics
        # Should not have bare "AKG" when a specific AKG model is listed.
        assert "AKG" not in mics
        assert any("414" in m for m in mics)

    def test_bare_family_kept_when_no_specific_model(self):
        mics = detect_source("recorded with AKG mics").mics
        assert "AKG" in mics

    def test_dpa_number(self):
        mics = detect_source("DPA 4061").mics
        assert any("DPA 4061" in m for m in mics)

    def test_no_mics(self):
        assert detect_source("gd1987-08-22.sbd").mics == []

    @pytest.mark.parametrize("text,wanted", [
        ("Schoeps CCM4V > Sound Devices 722", "Schoeps CCM4V"),
        ("Schoeps CMC641 cards", "Schoeps CMC641"),
        ("Sennheiser MD441 omni pair", "Sennheiser MD441"),
        ("Sony PCM-M10 internal", "Sony PCM-M10"),
        ("Sony TC-D5M cassette deck", "Sony TC-D5M"),
        ("Microtech Gefell M300 matched", "MT Gefell M300"),
        ("Milab VM-44 Link FOB", "Milab VM-44"),
        ("Earthworks SR40V cards", "Earthworks SR40V"),
        ("AKG CK91 capsule", "AKG CK91"),
        ("nakamichi CR-7A deck", "Nak CR-7A"),
        ("nak100 mics", "Nak 100"),
        ("nak1k pair", "Nak 1k"),
    ])
    def test_corpus_mined_mics(self, text: str, wanted: str):
        mics = detect_source(text).mics
        assert any(wanted in m for m in mics), f"{wanted!r} not in {mics}"


class TestRecorderDetection:
    """Recorders/preamps surfaced from the etree corpus -- they aren't mics
    strictly but they belong in the rig identifier label."""

    @pytest.mark.parametrize("text,wanted", [
        ("Tascam DR-680 24/96", "Tascam DR-680"),
        ("Tascam DA-3000", "Tascam DA-3000"),
        ("Tascam HD-P2 portable", "Tascam HD-P2"),
        ("Edirol R-44 16/48", "Edirol R-44"),
        ("Edirol R-09HR", "Edirol R-09HR"),
        ("Edirol UA-5 preamp", "Edirol UA-5"),
        ("Zoom H6 internal", "Zoom H6"),
        ("Zoom F6 32-bit float", "Zoom F6"),
        ("Sound Devices 722", "SD 722"),
    ])
    def test_recorder_models(self, text: str, wanted: str):
        mics = detect_source(text).mics
        assert any(wanted in m for m in mics), f"{wanted!r} not in {mics}"


class TestCombined:
    def test_kind_and_mics_together(self):
        src = detect_source("aud recording, Schoeps MK41")
        assert src.kind == "AUD"
        assert any("MK41" in m for m in src.mics)

    def test_multiple_sources_merged(self):
        src = detect_source("folder name", "01 track.flac", "info.txt body sbd Schoeps MK4")
        assert src.kind == "SBD"
        assert any("Schoeps MK4" in m for m in src.mics)

    def test_label_format(self):
        src = detect_source("sbd Schoeps MK4")
        assert src.label() == "[SBD Schoeps MK4]"

    def test_empty_label_when_nothing_detected(self):
        assert detect_source("random").label() == ""
