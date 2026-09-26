"""Tests for pipeline planning.

``build_plan`` is a pure function, so the complete matrix of recording modes can
be verified without a display server, a sound card or a running pipeline. This
is where the requirement "all mandatory modes are supported" is actually
enforced.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from troika.config import RecordingConfig
from troika.modes import AudioMode, FrameRate, RecordingType
from troika.pipeline import (
    AUDIO_CAPS,
    AUDIO_RATE,
    SOFTWARE_H264_ENCODER,
    build_plan,
    describe_plan,
)


def plan_for(config: RecordingConfig, path: str = "/tmp/out"):
    return build_plan(
        config,
        Path(path),
        system_audio_device="monitor.source",
        prefer_hardware=False,
    )


# -- mandatory recording modes ------------------------------------------------


def test_video_only_has_no_audio_branch(tmp_recording_dir: Path) -> None:
    plan = plan_for(
        RecordingConfig(
            recording_type=RecordingType.SCREEN,
            frame_rate=FrameRate.FPS_15,
            audio_mode=AudioMode.NONE,
            output_dir=tmp_recording_dir,
        )
    )
    assert plan.has_video is True
    assert plan.has_audio is False
    assert plan.audio_sources == ()


def test_video_with_system_audio_uses_a_monitor(tmp_recording_dir: Path) -> None:
    plan = plan_for(
        RecordingConfig(
            recording_type=RecordingType.SCREEN,
            audio_mode=AudioMode.SYSTEM,
            output_dir=tmp_recording_dir,
        )
    )
    assert len(plan.audio_sources) == 1
    source = plan.audio_sources[0]
    assert source.role == "system"
    assert source.device == "monitor.source"
    # With no microphone present, system audio must own the clock.
    assert source.provides_clock is True


def test_video_with_microphone_uses_the_selected_device(tmp_recording_dir: Path) -> None:
    plan = plan_for(
        RecordingConfig(
            recording_type=RecordingType.SCREEN,
            audio_mode=AudioMode.MICROPHONE,
            microphone="usb-mic",
            output_dir=tmp_recording_dir,
        )
    )
    assert len(plan.audio_sources) == 1
    assert plan.audio_sources[0].device == "usb-mic"
    assert plan.audio_sources[0].provides_clock is True


def test_video_with_both_audio_sources_mixes_two_branches(tmp_recording_dir: Path) -> None:
    plan = plan_for(
        RecordingConfig(
            recording_type=RecordingType.SCREEN,
            audio_mode=AudioMode.SYSTEM_AND_MICROPHONE,
            microphone="usb-mic",
            output_dir=tmp_recording_dir,
        )
    )
    roles = {source.role for source in plan.audio_sources}
    assert roles == {"microphone", "system"}
    assert plan.audio_is_mixed is True
    # Exactly one branch may provide the pipeline clock, and it must be the mic.
    clock_providers = [s for s in plan.audio_sources if s.provides_clock]
    assert len(clock_providers) == 1
    assert clock_providers[0].role == "microphone"


def test_audio_only_records_just_the_microphone(tmp_recording_dir: Path) -> None:
    plan = plan_for(
        RecordingConfig(
            recording_type=RecordingType.AUDIO,
            microphone="internal-mic",
            output_dir=tmp_recording_dir,
        )
    )
    assert plan.has_video is False
    assert len(plan.audio_sources) == 1
    assert plan.audio_sources[0].device == "internal-mic"
    assert plan.video_source is None


# -- frame rates --------------------------------------------------------------


@pytest.mark.parametrize("fps", [FrameRate.FPS_15, FrameRate.FPS_30])
def test_both_frame_rates_are_planned(fps, tmp_recording_dir: Path) -> None:
    plan = plan_for(
        RecordingConfig(
            recording_type=RecordingType.SCREEN,
            frame_rate=fps,
            audio_mode=AudioMode.NONE,
            output_dir=tmp_recording_dir,
        )
    )
    assert plan.frame_rate == int(fps.value)


# -- container and codec choices ---------------------------------------------


def test_video_uses_matroskamux_and_audio_uses_oggmux(tmp_recording_dir: Path) -> None:
    video_plan = plan_for(
        RecordingConfig(recording_type=RecordingType.SCREEN, output_dir=tmp_recording_dir),
        "/tmp/v.mkv",
    )
    audio_plan = plan_for(
        RecordingConfig(
            recording_type=RecordingType.AUDIO,
            microphone="mic",
            output_dir=tmp_recording_dir,
        ),
        "/tmp/a.ogg",
    )
    assert video_plan.container == "matroskamux"
    assert audio_plan.container == "oggmux"


def test_extension_is_corrected_to_match_the_container(tmp_recording_dir: Path) -> None:
    plan = plan_for(
        RecordingConfig(
            recording_type=RecordingType.AUDIO,
            microphone="mic",
            output_dir=tmp_recording_dir,
        ),
        "/tmp/misnamed.mp4",
    )
    assert plan.output_path.suffix == ".ogg"


def test_software_fallback_when_hardware_is_unavailable(tmp_recording_dir: Path) -> None:
    # prefer_hardware=False is the documented safe path when no GPU is usable.
    plan = plan_for(
        RecordingConfig(
            recording_type=RecordingType.SCREEN, output_dir=tmp_recording_dir
        )
    )
    assert plan.video_encoder == SOFTWARE_H264_ENCODER
    assert plan.hardware_encoder is False


def test_test_sources_never_claim_hardware_encoding(tmp_recording_dir: Path) -> None:
    # The self-test validates the software path, so it must not be reported as
    # hardware validation.
    plan = build_plan(
        RecordingConfig(
            recording_type=RecordingType.SCREEN, output_dir=tmp_recording_dir
        ),
        Path("/tmp/self.mkv"),
        use_test_sources=True,
        prefer_hardware=True,
    )
    assert plan.video_source == "test"
    assert plan.hardware_encoder is False


# -- audio normalisation ------------------------------------------------------


def test_audio_caps_are_normalised_for_mixing(tmp_recording_dir: Path) -> None:
    # Every branch must converge on one format before mixing, otherwise the
    # mixer reports not-negotiated when the devices disagree.
    assert f"rate={AUDIO_RATE}" in AUDIO_CAPS
    assert "channels=2" in AUDIO_CAPS
    assert "format=S16LE" in AUDIO_CAPS


def test_audio_encoder_is_opus_for_both_containers(tmp_recording_dir: Path) -> None:
    plan = plan_for(
        RecordingConfig(
            recording_type=RecordingType.AUDIO,
            microphone="mic",
            output_dir=tmp_recording_dir,
        )
    )
    assert plan.audio_encoder == "opusenc"


# -- introspection ------------------------------------------------------------


def test_describe_plan_reports_everything_diagnostics_need(tmp_recording_dir: Path) -> None:
    plan = plan_for(
        RecordingConfig(
            recording_type=RecordingType.SCREEN,
            frame_rate=FrameRate.FPS_30,
            audio_mode=AudioMode.SYSTEM_AND_MICROPHONE,
            microphone="usb-mic",
            output_dir=tmp_recording_dir,
        ),
        "/tmp/full.mkv",
    )
    described = describe_plan(plan)

    assert described["container"] == "matroskamux"
    assert described["frame_rate"] == 30
    assert described["video_encoder"] == SOFTWARE_H264_ENCODER
    assert described["audio_mixed"] is True
    assert {s["role"] for s in described["audio_sources"]} == {"microphone", "system"}
    assert described["audio_caps"] == AUDIO_CAPS


def test_plan_requires_a_valid_configuration(tmp_recording_dir: Path) -> None:
    with pytest.raises(Exception):
        build_plan(
            RecordingConfig(
                recording_type=RecordingType.SCREEN,
                audio_mode=AudioMode.MICROPHONE,
                microphone=None,
                output_dir=tmp_recording_dir,
            ),
            Path("/tmp/x.mkv"),
            prefer_hardware=False,
        )
