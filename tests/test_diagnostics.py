"""Tests for recording diagnostics.

The counters are what the acceptance tests use to answer "were there
interruptions?", so the gap detection itself needs to be correct: a recording
with a good average frame rate can still contain a two-second freeze.
"""

from __future__ import annotations

from troika.diagnostics import (
    AUDIO_GAP_THRESHOLD_NS,
    VIDEO_GAP_THRESHOLD_NS,
    Diagnostics,
    StreamStats,
)

MS = 1_000_000


def test_disabled_diagnostics_attach_no_probes() -> None:
    diagnostics = Diagnostics(enabled=False)

    class Pipeline:
        def get_by_name(self, _name):  # pragma: no cover - must not be called
            raise AssertionError("disabled diagnostics must not touch the pipeline")

    diagnostics.attach(Pipeline())  # must be a no-op
    assert diagnostics.streams == {}


def _gst():
    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst

    if not Gst.is_initialized():
        Gst.init(None)
    return Gst


def test_enabled_diagnostics_probe_the_muxer_queues() -> None:
    """Probes must land on the muxer feeds, so each frame is counted once."""
    Gst = _gst()
    pipeline = Gst.Pipeline.new("diagnostics-test")
    probed: list[str] = []

    class _Pad:
        def __init__(self, name):
            self._name = name

        def add_probe(self, probe_type, _callback, *_args):
            assert probe_type == Gst.PadProbeType.BUFFER
            probed.append(self._name)

    class _Element:
        def __init__(self, name):
            self._name = name

        def get_static_pad(self, _name):
            return _Pad(self._name)

    class _Pipeline:
        def get_by_name(self, name):
            return _Element(name) if name.endswith("_mux_queue") else None

    diagnostics = Diagnostics(enabled=True)
    diagnostics.attach(_Pipeline())

    assert set(probed) == {"video_mux_queue", "audio_mux_queue"}


def test_steady_stream_reports_no_gaps() -> None:
    stats = StreamStats(name="video_src", _is_video=True)
    # 30 fps for one second.
    for index in range(30):
        stats.observe(index * 33 * MS, 1000)

    assert stats.buffers == 30
    assert stats.gaps == []
    assert stats.effective_fps is not None
    assert 29.0 < stats.effective_fps < 31.0


def test_a_freeze_is_recorded_as_a_gap() -> None:
    stats = StreamStats(name="video_src", _is_video=True)
    stats.observe(0, 1000)
    stats.observe(33 * MS, 1000)
    # A two second freeze.
    stats.observe(2_033 * MS, 1000)

    assert len(stats.gaps) == 1
    assert stats.gaps[0] > VIDEO_GAP_THRESHOLD_NS
    assert stats.max_gap_ns == 2_000 * MS


def test_short_audio_jitter_is_not_reported_as_a_gap() -> None:
    stats = StreamStats(name="audio_src")
    stats.observe(0, 100)
    stats.observe(20 * MS, 100)
    # A 100 ms hiccup is below the audible-dropout threshold.
    stats.observe(120 * MS, 100)

    assert stats.gaps == []
    assert stats.max_gap_ns == 100 * MS


def test_a_long_audio_silence_is_reported() -> None:
    stats = StreamStats(name="audio_src")
    stats.observe(0, 100)
    stats.observe(AUDIO_GAP_THRESHOLD_NS + 50 * MS, 100)

    assert len(stats.gaps) == 1


def test_audio_and_video_use_different_thresholds() -> None:
    # Video is sampled, so a 300 ms gap is one dropped frame at 15 fps; audio is
    # continuous, so 300 ms is a hole. The thresholds must differ.
    assert VIDEO_GAP_THRESHOLD_NS != AUDIO_GAP_THRESHOLD_NS
    assert AUDIO_GAP_THRESHOLD_NS < VIDEO_GAP_THRESHOLD_NS


def test_out_of_order_timestamps_are_ignored_not_counted() -> None:
    stats = StreamStats(name="video_src", _is_video=True)
    stats.observe(0, 1000)
    stats.observe(100 * MS, 1000)
    stats.observe(50 * MS, 1000)  # a late buffer, normal around startup

    assert stats.gaps == []


def test_buffers_without_a_timestamp_are_counted_but_skipped() -> None:
    stats = StreamStats(name="audio_src")
    stats.observe(None, 100)
    stats.observe(0, 100)
    stats.observe(20 * MS, 100)

    assert stats.buffers == 3
    assert stats.first_pts_ns == 0
    assert stats.gaps == []


def test_effective_fps_needs_at_least_two_buffers() -> None:
    stats = StreamStats(name="video_src", _is_video=True)
    assert stats.effective_fps is None
    stats.observe(0, 1000)
    assert stats.effective_fps is None


def test_diagnostics_summary_reports_each_stream_and_its_gaps() -> None:
    diagnostics = Diagnostics(enabled=True)
    diagnostics.start()
    diagnostics.note("video encoder", "x264enc")
    diagnostics.observe("video_src", True, 0, 1000)
    diagnostics.observe("video_src", True, 33 * MS, 1000)
    diagnostics.observe("video_src", True, 2_033 * MS, 1000)
    diagnostics.stop()

    summary = diagnostics.summary()

    assert summary["notes"]["video encoder"] == "x264enc"
    assert "video_src" in summary["streams"]
    assert summary["streams"]["video_src"]["gaps_over_threshold"] == 1
    assert summary["streams"]["video_src"]["largest_gaps_ms"]


def test_formatted_summary_is_concise_and_mentions_the_wall_clock() -> None:
    diagnostics = Diagnostics(enabled=True)
    diagnostics.start()
    diagnostics.observe("video_src", True, 0, 1000)
    diagnostics.stop()

    text = diagnostics.format_summary()

    assert "Troika D Lite diagnostics" in text
    assert "wall clock" in text
    assert "video_src" in text


def test_messages_are_only_recorded_when_enabled() -> None:
    enabled = Diagnostics(enabled=True)
    enabled.note_message("something went wrong")
    assert enabled.summary()["warnings"] == {"something went wrong": 1}

    disabled = Diagnostics(enabled=False)
    disabled.note_message("something went wrong")
    assert disabled.summary()["warnings"] == {}


def test_wall_duration_grows_while_recording() -> None:
    diagnostics = Diagnostics(enabled=True)
    diagnostics.start()
    assert diagnostics.wall_duration >= 0
    diagnostics.stop()
    assert diagnostics.wall_duration >= 0
