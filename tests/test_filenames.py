"""Tests for output filename generation and destination checks."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

import pytest

from troika.config import ConfigError
from troika.filenames import (
    check_free_space,
    ensure_writable_directory,
    extension_for,
    generate_filename,
    unique_output_path,
)
from troika.modes import RecordingType


def test_video_uses_matroska_and_audio_uses_ogg() -> None:
    # The container choice is a documented design decision, so it is pinned.
    assert extension_for(RecordingType.SCREEN) == "mkv"
    assert extension_for(RecordingType.AUDIO) == "ogg"


def test_filename_is_timestamped() -> None:
    when = datetime(2026, 9, 25, 18, 30, 0)
    name = generate_filename(RecordingType.SCREEN, when)
    assert name == "Troika-D-Lite_2026-09-25_18-30-00.mkv"


def test_audio_filename_uses_the_ogg_extension() -> None:
    when = datetime(2026, 1, 2, 3, 4, 5)
    assert generate_filename(RecordingType.AUDIO, when).endswith(".ogg")


def test_existing_recording_is_never_overwritten(tmp_recording_dir: Path) -> None:
    when = datetime(2026, 9, 25, 18, 30, 0)
    first = unique_output_path(tmp_recording_dir, RecordingType.SCREEN, when)
    first.write_bytes(b"existing recording")

    second = unique_output_path(tmp_recording_dir, RecordingType.SCREEN, when)

    assert second != first
    assert second.exists() is False
    assert first.read_bytes() == b"existing recording"


def test_repeated_collisions_keep_producing_new_names(tmp_recording_dir: Path) -> None:
    when = datetime(2026, 9, 25, 18, 30, 0)
    paths = []
    for _ in range(4):
        path = unique_output_path(tmp_recording_dir, RecordingType.SCREEN, when)
        path.write_bytes(b"x")
        paths.append(path)
    assert len(set(paths)) == 4


def test_writable_directory_is_created(tmp_path: Path) -> None:
    target = tmp_path / "a" / "b" / "c"
    ensure_writable_directory(target)
    assert target.is_dir()


def test_unwritable_directory_is_reported(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("running as root, so permission checks do not apply")
    target = tmp_path / "readonly"
    target.mkdir()
    target.chmod(0o500)
    try:
        with pytest.raises(ConfigError, match="not writable"):
            ensure_writable_directory(target)
    finally:
        target.chmod(0o700)


def test_a_file_where_a_directory_is_expected_is_reported(tmp_path: Path) -> None:
    blocking = tmp_path / "not_a_dir"
    blocking.write_text("x")
    with pytest.raises(ConfigError):
        ensure_writable_directory(blocking)


def test_free_space_check_passes_on_a_normal_filesystem(tmp_recording_dir: Path) -> None:
    check_free_space(tmp_recording_dir, minimum_bytes=1)


def test_free_space_check_rejects_an_impossible_requirement(tmp_recording_dir: Path) -> None:
    with pytest.raises(ConfigError, match="free space"):
        check_free_space(tmp_recording_dir, minimum_bytes=1 << 60)
