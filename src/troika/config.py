"""Validated recording configuration.

``RecordingConfig`` is the single value object the UI edits and the pipeline
builder consumes. Keeping validation here means the pipeline builder never has
to second-guess its inputs, and it means the whole selection logic is testable
without a display server or a media pipeline.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path

from .modes import (
    DEFAULT_AUDIO_MODE,
    DEFAULT_FRAME_RATE,
    DEFAULT_RECORDING_TYPE,
    AudioMode,
    FrameRate,
    RecordingType,
)


class ConfigError(ValueError):
    """Raised when a configuration cannot be recorded."""


def default_output_dir() -> Path:
    """Return the default recording destination (the user's Videos folder)."""
    xdg = os.environ.get("XDG_VIDEOS_DIR")
    if xdg:
        return Path(xdg)
    home = Path.home()
    videos = home / "Videos"
    return videos if videos.parent.exists() else home


@dataclass(frozen=True)
class RecordingConfig:
    """A complete, self-consistent description of one recording."""

    recording_type: RecordingType = DEFAULT_RECORDING_TYPE
    frame_rate: FrameRate = DEFAULT_FRAME_RATE
    audio_mode: AudioMode = DEFAULT_AUDIO_MODE
    microphone: str | None = None
    output_dir: Path = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.output_dir is None:
            object.__setattr__(self, "output_dir", default_output_dir())
        elif not isinstance(self.output_dir, Path):
            object.__setattr__(self, "output_dir", Path(self.output_dir))
        # Accept plain ints/strings from callers (and tests) and normalise them,
        # so the rest of the program can rely on the enums.
        try:
            object.__setattr__(self, "frame_rate", FrameRate(self.frame_rate))
        except ValueError as exc:
            raise ConfigError(f"Unsupported frame rate: {self.frame_rate!r}") from exc
        try:
            object.__setattr__(self, "audio_mode", AudioMode(self.audio_mode))
        except ValueError as exc:
            raise ConfigError(f"Unsupported audio mode: {self.audio_mode!r}") from exc
        try:
            object.__setattr__(self, "recording_type", RecordingType(self.recording_type))
        except ValueError as exc:
            raise ConfigError(
                f"Unsupported recording type: {self.recording_type!r}"
            ) from exc

    # -- derived properties -------------------------------------------------

    @property
    def records_video(self) -> bool:
        return self.recording_type is RecordingType.SCREEN

    @property
    def records_audio(self) -> bool:
        if self.recording_type is RecordingType.AUDIO:
            return True
        return self.audio_mode.wants_audio

    @property
    def wants_system_audio(self) -> bool:
        return self.audio_mode.wants_system_audio

    @property
    def wants_microphone(self) -> bool:
        return self.audio_mode.wants_microphone

    # -- validation ---------------------------------------------------------

    def validate(self) -> None:
        """Raise :class:`ConfigError` if this configuration cannot be recorded."""
        if self.recording_type is RecordingType.AUDIO:
            # Audio-only always records the microphone; the audio mode selector
            # only applies to screen recordings.
            if not self.microphone:
                raise ConfigError(
                    "Select a microphone before starting an audio-only recording."
                )
        elif self.wants_microphone and not self.microphone:
            raise ConfigError(
                "Select a microphone, or choose an audio option that does not "
                "need one."
            )

        if not self.records_video and not self.records_audio:
            raise ConfigError("Nothing to record: select video and/or audio.")

    def with_changes(self, **kwargs) -> "RecordingConfig":
        """Return a copy of this configuration with the given fields replaced."""
        return replace(self, **kwargs)
