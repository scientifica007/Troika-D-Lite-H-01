"""Tests for configuration validation and derived behaviour."""

from __future__ import annotations

from pathlib import Path

import pytest

from troika.config import ConfigError, RecordingConfig, default_output_dir
from troika.modes import AudioMode, FrameRate, RecordingType


def test_screen_recording_without_audio_is_valid(tmp_recording_dir: Path) -> None:
    config = RecordingConfig(
        recording_type=RecordingType.SCREEN,
        frame_rate=FrameRate.FPS_30,
        audio_mode=AudioMode.NONE,
        output_dir=tmp_recording_dir,
    )
    config.validate()  # should not raise

    assert config.records_video is True
    assert config.records_audio is False


def test_audio_only_requires_a_microphone(tmp_recording_dir: Path) -> None:
    config = RecordingConfig(
        recording_type=RecordingType.AUDIO,
        audio_mode=AudioMode.NONE,
        output_dir=tmp_recording_dir,
    )
    with pytest.raises(ConfigError, match="microphone"):
        config.validate()


def test_audio_only_is_valid_with_a_microphone(tmp_recording_dir: Path) -> None:
    config = RecordingConfig(
        recording_type=RecordingType.AUDIO,
        microphone="alsa_input.usb",
        output_dir=tmp_recording_dir,
    )
    config.validate()

    # Audio-only always captures audio even though the audio mode says "none".
    assert config.records_video is False
    assert config.records_audio is True


@pytest.mark.parametrize(
    "mode, wants_mic, wants_system",
    [
        (AudioMode.NONE, False, False),
        (AudioMode.SYSTEM, False, True),
        (AudioMode.MICROPHONE, True, False),
        (AudioMode.SYSTEM_AND_MICROPHONE, True, True),
    ],
)
def test_audio_mode_source_selection(mode, wants_mic, wants_system) -> None:
    config = RecordingConfig(audio_mode=mode, microphone="mic")
    assert config.wants_microphone is wants_mic
    assert config.wants_system_audio is wants_system


def test_microphone_mode_requires_a_microphone(tmp_recording_dir: Path) -> None:
    config = RecordingConfig(
        recording_type=RecordingType.SCREEN,
        audio_mode=AudioMode.MICROPHONE,
        output_dir=tmp_recording_dir,
    )
    with pytest.raises(ConfigError):
        config.validate()


def test_system_audio_mode_does_not_require_a_microphone(tmp_recording_dir: Path) -> None:
    config = RecordingConfig(
        recording_type=RecordingType.SCREEN,
        audio_mode=AudioMode.SYSTEM,
        output_dir=tmp_recording_dir,
    )
    config.validate()  # system audio uses a monitor, not a microphone


def test_plain_values_are_normalised_to_enums(tmp_recording_dir: Path) -> None:
    # The UI and tests both pass plain values; the config normalises them so the
    # rest of the program can rely on enums.
    config = RecordingConfig(
        recording_type="screen",
        frame_rate=15,
        audio_mode="microphone",
        microphone="mic",
        output_dir=tmp_recording_dir,
    )
    assert config.recording_type is RecordingType.SCREEN
    assert config.frame_rate is FrameRate.FPS_15
    assert config.audio_mode is AudioMode.MICROPHONE


def test_invalid_frame_rate_is_rejected(tmp_recording_dir: Path) -> None:
    with pytest.raises(ConfigError, match="frame rate"):
        RecordingConfig(frame_rate=60, output_dir=tmp_recording_dir)


def test_invalid_audio_mode_is_rejected(tmp_recording_dir: Path) -> None:
    with pytest.raises(ConfigError, match="audio mode"):
        RecordingConfig(audio_mode="everything", output_dir=tmp_recording_dir)


def test_string_output_dir_is_coerced_to_path(tmp_recording_dir: Path) -> None:
    config = RecordingConfig(output_dir=str(tmp_recording_dir))
    assert isinstance(config.output_dir, Path)
    assert config.output_dir == tmp_recording_dir


def test_with_changes_returns_an_independent_copy(tmp_recording_dir: Path) -> None:
    original = RecordingConfig(
        frame_rate=FrameRate.FPS_15, microphone="a", output_dir=tmp_recording_dir
    )
    changed = original.with_changes(frame_rate=FrameRate.FPS_30)

    assert original.frame_rate is FrameRate.FPS_15
    assert changed.frame_rate is FrameRate.FPS_30
    assert changed.microphone == "a"


def test_default_output_dir_is_a_path() -> None:
    assert isinstance(default_output_dir(), Path)
