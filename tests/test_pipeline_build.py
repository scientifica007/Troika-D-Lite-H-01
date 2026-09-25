"""Pipeline construction tests against real GStreamer.

``test_pipeline_plan.py`` covers the pure planning logic. These tests go one
step further and build the actual pipeline, because two defects that only exist
once real elements are linked — a probe type that is not a ``Gst.PadProbeType``
and an ``audiomixer`` that collapses a two-source mix to mono — passed the
planning tests unnoticed.

Generated sources (``videotestsrc``/``audiotestsrc``) are used throughout, so no
display server, microphone or portal is required.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from troika import pipeline as pipeline_module
from troika.diagnostics import Diagnostics
from troika.pipeline import (
    AUDIO_CAPS,
    AudioSourcePlan,
    PipelinePlan,
    build_pipeline,
)

pytestmark = pytest.mark.skipif(
    pipeline_module._require_gst() is None, reason="GStreamer is not available"
)


def _video_plan(output: Path, fps: int = 15) -> PipelinePlan:
    return PipelinePlan(
        output_path=output,
        container="matroskamux",
        video_source="test",
        frame_rate=fps,
    )


def _audio_plan(output: Path, sources: tuple[AudioSourcePlan, ...]) -> PipelinePlan:
    return PipelinePlan(
        output_path=output,
        container="matroskamux",
        video_source=None,
        frame_rate=None,
        audio_sources=sources,
    )


def _microphone(test_format: str | None = None) -> AudioSourcePlan:
    return AudioSourcePlan(
        role="microphone",
        element="audiotestsrc",
        device=None,
        provides_clock=True,
        test_format=test_format,
    )


def _system_audio(test_format: str | None = None) -> AudioSourcePlan:
    return AudioSourcePlan(
        role="system",
        element="audiotestsrc",
        device=None,
        provides_clock=False,
        test_format=test_format,
    )


def _negotiated_caps(pipeline, element_name: str, pad_name: str = "sink", timeout: float = 15.0):
    """Run *pipeline* until the named pad has negotiated caps.

    The capture sources are live, so the pipeline never prerolls: caps only
    appear once buffers actually flow. Polling here is a test-only convenience;
    the application itself never polls.
    """
    Gst = pipeline_module._require_gst()
    import time

    element = pipeline.get_by_name(element_name)
    assert element is not None, f"no element named {element_name}"
    pad = element.get_static_pad(pad_name)
    assert pad is not None, f"{element_name} has no {pad_name} pad"

    result = pipeline.set_state(Gst.State.PLAYING)
    if result == Gst.StateChangeReturn.FAILURE:
        raise AssertionError("pipeline failed to start")

    deadline = time.monotonic() + timeout
    caps = pad.get_current_caps()
    while caps is None and time.monotonic() < deadline:
        time.sleep(0.05)
        caps = pad.get_current_caps()
    return caps


def _caps_of(pipeline, element_name: str, pad_name: str = "sink", timeout: float = 15.0):
    """Return negotiated caps as a normalised ``key=value`` string."""
    caps = _negotiated_caps(pipeline, element_name, pad_name, timeout)
    assert caps is not None, f"{element_name}.{pad_name} never negotiated caps"
    # Gst's to_string() adds spaces and type annotations; normalise so the
    # assertions read like the caps strings in pipeline.py.
    return (
        caps.to_string()
        .replace(" ", "")
        .replace("(int)", "")
        .replace("(string)", "")
        .replace("(fraction)", "")
    )


def test_video_only_pipeline_builds_and_negotiates(tmp_path: Path) -> None:
    Gst = pipeline_module._require_gst()
    pipeline = build_pipeline(_video_plan(tmp_path / "video.mkv"))

    assert pipeline.get_by_name("video_src") is not None
    assert pipeline.get_by_name("video_encoder") is not None

    try:
        # The encoder must be fed I420 so the result is a standard 4:2:0 stream.
        text = _caps_of(pipeline, "video_encoder")
        assert "video/x-raw" in text
        assert "format=I420" in text
    finally:
        pipeline.set_state(Gst.State.NULL)


def test_video_rate_caps_pin_the_requested_fps(tmp_path: Path) -> None:
    Gst = pipeline_module._require_gst()
    pipeline = build_pipeline(_video_plan(tmp_path / "video.mkv", fps=30))
    try:
        assert "framerate=30/1" in _caps_of(pipeline, "video_encoder")
    finally:
        pipeline.set_state(Gst.State.NULL)


def test_audio_only_pipeline_builds(tmp_path: Path) -> None:
    Gst = pipeline_module._require_gst()
    pipeline = build_pipeline(_audio_plan(tmp_path / "audio.mkv", (_microphone(),)))
    assert pipeline.get_by_name("audio_encoder") is not None

    try:
        _negotiated_caps(pipeline, "audio_encoder")
    finally:
        pipeline.set_state(Gst.State.NULL)


def test_single_audio_source_is_encoded_at_the_normalised_format(
    tmp_path: Path,
) -> None:
    Gst = pipeline_module._require_gst()
    pipeline = build_pipeline(
        _audio_plan(tmp_path / "audio.mkv", (_microphone(test_format="44100:1"),))
    )
    try:
        text = _caps_of(pipeline, "audio_encoder")
        # A 44.1 kHz mono source must arrive at the encoder normalised.
        assert "rate=48000" in text
        assert "channels=2" in text
    finally:
        pipeline.set_state(Gst.State.NULL)


def test_mixed_audio_stays_stereo(tmp_path: Path) -> None:
    """A two-source mix must not be silently downmixed to mono.

    ``audiomixer`` picks its output channel count from downstream, so without an
    explicit capsfilter after it a system+microphone recording came out mono.
    """
    Gst = pipeline_module._require_gst()
    pipeline = build_pipeline(
        _audio_plan(
            tmp_path / "mixed.mkv",
            (
                _microphone(test_format="44100:1"),
                _system_audio(test_format="48000:2"),
            ),
        )
    )
    assert pipeline.get_by_name("audio_mixer") is not None

    try:
        text = _caps_of(pipeline, "audio_encoder")
        assert "channels=2" in text
        assert "rate=48000" in text
    finally:
        pipeline.set_state(Gst.State.NULL)


def test_mixed_audio_has_an_explicit_output_capsfilter(tmp_path: Path) -> None:
    pipeline = build_pipeline(
        _audio_plan(
            tmp_path / "mixed.mkv",
            (_microphone(), _system_audio()),
        )
    )
    caps_element = pipeline.get_by_name("mixed_audio_caps")
    assert caps_element is not None
    caps = caps_element.get_property("caps")
    normalised = (
        caps.to_string()
        .replace(" ", "")
        .replace("(int)", "")
        .replace("(string)", "")
    )
    assert normalised == AUDIO_CAPS


def test_two_source_plan_is_reported_as_mixed(tmp_path: Path) -> None:
    plan = _audio_plan(
        tmp_path / "mixed.mkv", (_microphone(), _system_audio())
    )
    assert plan.audio_is_mixed is True
    assert plan.audio_encoder == "opusenc"


def test_diagnostics_probes_attach_to_a_real_pipeline(tmp_path: Path) -> None:
    """Regression: the probe type must be a ``Gst.PadProbeType``, not an int.

    ``attach`` used to pass a plain ``1``, which raised ``TypeError`` the moment
    a real recording with diagnostics enabled started.
    """
    Gst = pipeline_module._require_gst()
    pipeline = build_pipeline(_video_plan(tmp_path / "video.mkv"))
    diagnostics = Diagnostics(enabled=True)

    diagnostics.attach(pipeline)  # must not raise

    # Run a few frames so the probes actually fire, then confirm they counted.
    pipeline.set_state(Gst.State.PLAYING)
    deadline = 20
    while deadline and not diagnostics.streams:
        import time

        time.sleep(0.1)
        deadline -= 1
    pipeline.set_state(Gst.State.NULL)

    assert "video" in diagnostics.streams
    assert diagnostics.streams["video"].buffers > 0


def test_disabled_diagnostics_leave_a_real_pipeline_untouched(
    tmp_path: Path,
) -> None:
    pipeline = build_pipeline(_video_plan(tmp_path / "video.mkv"))
    diagnostics = Diagnostics(enabled=False)

    diagnostics.attach(pipeline)

    assert diagnostics.streams == {}


def test_a_failed_link_raises_a_useful_error(tmp_path: Path) -> None:
    """A plan asking for an element that does not exist must not crash the app."""
    plan = PipelinePlan(
        output_path=tmp_path / "broken.mkv",
        container="matroskamux",
        video_source="test",
        frame_rate=15,
        video_encoder="no_such_encoder_element",
    )
    with pytest.raises(RuntimeError) as excinfo:
        build_pipeline(plan)
    assert "no_such_encoder_element" in str(excinfo.value)


def test_a_plan_with_no_streams_is_rejected(tmp_path: Path) -> None:
    plan = PipelinePlan(
        output_path=tmp_path / "empty.mkv",
        container="matroskamux",
        video_source=None,
        frame_rate=None,
    )
    with pytest.raises(RuntimeError):
        build_pipeline(plan)
