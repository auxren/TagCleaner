"""Shared pytest fixtures for TagCleaner's test suite."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def silent_flac() -> Path:
    """Path to a real 0.1s silent FLAC file. Tests should copy from this."""
    src = FIXTURES / "silent.flac"
    assert src.exists(), f"missing fixture {src}"
    return src


@pytest.fixture
def silent_mp3() -> Path:
    src = FIXTURES / "silent.mp3"
    assert src.exists(), f"missing fixture {src}"
    return src


@pytest.fixture
def silent_wav() -> Path:
    src = FIXTURES / "silent.wav"
    assert src.exists(), f"missing fixture {src}"
    return src


@pytest.fixture
def make_flac(tmp_path, silent_flac):
    """Factory: copy the silent FLAC fixture to *dest* and return the Path."""

    def _make(dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(silent_flac, dest)
        return dest

    return _make


@pytest.fixture
def make_mp3(tmp_path, silent_mp3):
    def _make(dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(silent_mp3, dest)
        return dest

    return _make


@pytest.fixture
def make_wav(tmp_path, silent_wav):
    def _make(dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(silent_wav, dest)
        return dest

    return _make


@pytest.fixture
def make_concert_tree(tmp_path, make_flac):
    """Factory that builds a fake concert folder on disk.

    Usage::

        folder = make_concert_tree(
            "rush1984-09-21.sbd.fear.flac16",
            audio=["01 Spirit of Radio.flac", "02 Enemy Within.flac"],
            info_txt=("Rush 1984-09-21 - Fear.txt", "Rush\\n1984-09-21\\n..."),
        )

    Returns the created folder Path. Pass ``nested=True`` to place audio
    one level deeper (the folder/folder/*.flac pattern).
    """

    def _make(
        folder_name: str,
        *,
        audio: list[str],
        info_txt: tuple[str, str] | None = None,
        nested: bool = False,
        root: Path | None = None,
    ) -> Path:
        root = root or tmp_path
        folder = root / folder_name
        folder.mkdir(parents=True, exist_ok=True)
        host = folder
        if nested:
            host = folder / folder_name
            host.mkdir()
        for name in audio:
            make_flac(host / name)
        if info_txt is not None:
            name, body = info_txt
            (host / name).write_text(body, encoding="utf-8")
        return folder

    return _make
