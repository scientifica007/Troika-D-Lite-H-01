"""Recording mode definitions.

These enumerations are the vocabulary the UI, the configuration and the
pipeline builder all share. They are deliberately small: the product records
audio, or a screen with one of a few audio combinations, at one of two frame
rates. Nothing else.
"""

from __future__ import annotations

from enum import Enum


class RecordingType(str, Enum):
    """What the user wants to capture."""

    AUDIO = "audio"
    SCREEN = "screen"


class AudioMode(str, Enum):
    """Which audio sources are mixed into the recording."""

    NONE = "none"
    SYSTEM = "system"
    MICROPHONE = "microphone"
    SYSTEM_AND_MICROPHONE = "system_microphone"

    @property
    def wants_system_audio(self) -> bool:
        return self in (AudioMode.SYSTEM, AudioMode.SYSTEM_AND_MICROPHONE)

    @property
    def wants_microphone(self) -> bool:
        return self in (AudioMode.MICROPHONE, AudioMode.SYSTEM_AND_MICROPHONE)

    @property
    def wants_audio(self) -> bool:
        return self is not AudioMode.NONE


class FrameRate(int, Enum):
    """Supported screen capture frame rates."""

    FPS_15 = 15
    FPS_30 = 30

    @property
    def value_fraction(self) -> str:
        """The GStreamer framerate fraction, e.g. ``15/1``."""
        return f"{int(self.value)}/1"


DEFAULT_FRAME_RATE = FrameRate.FPS_30
DEFAULT_RECORDING_TYPE = RecordingType.SCREEN
DEFAULT_AUDIO_MODE = AudioMode.NONE
