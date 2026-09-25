"""Tests for the media self-test harness.

The self-test is the only thing that exercises the real GStreamer pipelines
without a desktop session, so it needs to be trustworthy: its report must fail
when a check fails, and its generated plans must genuinely cover every mandatory
recording mode.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import pytest

from troika import selftest
from troika.pipeline import SOFTWARE_H264_ENCODER
from troika.selftest import CheckResult, SelfTestReport, _test_plan

# -- report logic -------------------------------------------------------------


def test_an_empty_report_passes() -> None:
    assert SelfTestReport().passed is True


def test_a_failed_check_fails_the_report() -> None:
    report = SelfTestReport()
    report.add("video only", True, "ok")
    report.add("audio only", False, "no audio stream")

    assert report.passed is False


def test_the_report_marks_each_check() -> None:
    report = SelfTestReport()
    report.add("video only", True)
    report.add("audio only", False, "boom")

    text = report.format()

    assert "[PASS] video only" in text
    assert "[FAIL] audio only" in text
    assert "result: FAIL" in text


def test_the_report_lists_artifacts() -> None:
    report = SelfTestReport()
    report.add("video only", True)
    report.artifacts.append(Path("/tmp/video_15.mkv"))

    assert "/tmp/video_15.mkv" in report.format()


def test_multiline_detail_is_indented() -> None:
    report = SelfTestReport()
    report.add("video only", True, "line one\nline two")

    assert "       line one" in report.format()
    assert "       line two" in report.format()


# -- generated plans ----------------------------------------------------------


def test_a_video_plan_has_a_test_source_not_a_portal(tmp_path: Path) -> None:
    plan = _test_plan(tmp_path / "v.mkv", with_video=True, audio_modes=())

    # Crucially, the self-test never touches the portal or the screen.
    assert plan.video_source == "test"
    assert plan.container == "matroskamux"
    assert plan.audio_sources == ()


def test_an_audio_plan_uses_oggmux_and_has_no_video(tmp_path: Path) -> None:
    plan = _test_plan(
        tmp_path / "a.ogg", with_video=False, audio_modes=("microphone",)
    )

    assert plan.container == "oggmux"
    assert plan.video_source is None
    assert plan.frame_rate is None
    assert len(plan.audio_sources) == 1


def test_exactly_one_audio_source_provides_the_clock(tmp_path: Path) -> None:
    plan = _test_plan(
        tmp_path / "m.ogg", with_video=False, audio_modes=("microphone", "system")
    )

    clock_providers = [s for s in plan.audio_sources if s.provides_clock]
    assert len(clock_providers) == 1


def test_the_two_audio_sources_have_mismatched_formats(tmp_path: Path) -> None:
    # Realistic: a microphone and a monitor rarely agree on format. Mixing them
    # must therefore work without a format match.
    plan = _test_plan(
        tmp_path / "m.ogg", with_video=False, audio_modes=("microphone", "system")
    )

    formats = {s.test_format for s in plan.audio_sources}
    assert formats == {"48000:2", "44100:1"}


@pytest.mark.parametrize("fps", [15, 30])
def test_the_plan_records_the_requested_frame_rate(tmp_path: Path, fps: int) -> None:
    plan = _test_plan(
        tmp_path / "v.mkv", with_video=True, audio_modes=(), fps=fps
    )
    assert plan.frame_rate == fps


def test_the_self_test_always_uses_the_software_encoder(tmp_path: Path) -> None:
    # The self-test validates the path that is guaranteed to exist on any
    # machine, so it must not report a hardware encoder as validated.
    plan = _test_plan(tmp_path / "v.mkv", with_video=True, audio_modes=())
    assert plan.video_encoder == SOFTWARE_H264_ENCODER
    assert plan.hardware_encoder is False


# -- running the harness ------------------------------------------------------


@pytest.mark.skipif(
    os.environ.get("TROIKA_SKIP_MEDIA_TESTS") == "1",
    reason="media self-test disabled by TROIKA_SKIP_MEDIA_TESTS",
)
def test_the_media_self_test_passes(tmp_path: Path) -> None:
    """Run the real harness end to end with short recordings.

    This is the strongest automated check available without a Wayland session:
    it encodes and muxes video and audio, mixes two audio branches, and verifies
    that the files are parseable.
    """
    report = selftest.run_self_test(workdir=tmp_path, duration_seconds=2)

    assert report.passed, report.format()
    # Every mandatory mode is covered, so the report must be substantial.
    assert len(report.results) >= 6
    for artifact in report.artifacts:
        assert artifact.exists()
        assert artifact.stat().st_size > 0


@pytest.mark.skipif(
    os.environ.get("TROIKA_SKIP_MEDIA_TESTS") == "1",
    reason="media self-test disabled by TROIKA_SKIP_MEDIA_TESTS",
)
def test_self_test_output_contains_the_expected_streams(tmp_path: Path) -> None:
    report = selftest.run_self_test(workdir=tmp_path, duration_seconds=2)
    assert report.passed, report.format()

    full = tmp_path / "full.mkv"
    info = selftest.inspect_file(full)
    types = {stream["type"] for stream in info["streams"]}

    assert "video" in types
    assert any(t.startswith("audio") for t in types)
    # The mixed recording is one audio track, not two unmixed tracks.
    audio_tracks = [t for t in types if t.startswith("audio")]
    assert len(audio_tracks) == 1


@pytest.mark.skipif(
    os.environ.get("TROIKA_SKIP_MEDIA_TESTS") == "1",
    reason="media self-test disabled by TROIKA_SKIP_MEDIA_TESTS",
)
def test_a_mixed_recording_keeps_both_channels(tmp_path: Path) -> None:
    """A two-source mix must stay stereo.

    ``audiomixer`` derives its output layout from downstream, so without an
    explicit capsfilter it silently collapses a stereo mix to mono. This test
    pins the behaviour: the recorded file must still have two channels.
    """
    report = selftest.run_self_test(workdir=tmp_path, duration_seconds=2)
    assert report.passed, report.format()

    info = selftest.inspect_file(tmp_path / "full.mkv")
    audio = [s for s in info["streams"] if s["type"].startswith("audio")]
    assert audio, info
    assert audio[0]["channels"] == 2, audio[0]


@contextmanager
def _stubbed_media(tmp_path: Path):
    """Run the harness without GStreamer, to test its cleanup behaviour.

    Only the media calls are replaced. The temporary-folder logic under test is
    the real one, redirected into *tmp_path* so the test can see what survives.
    """

    def fake_mkdtemp(*args, **kwargs):
        prefix = kwargs.get("prefix") or (args[0] if args else "")
        path = tmp_path / f"{prefix}generated"
        path.mkdir(parents=True, exist_ok=True)
        return str(path)

    def fake_run(plan, duration_seconds=0):
        plan.output_path.parent.mkdir(parents=True, exist_ok=True)
        plan.output_path.write_bytes(b"stub")
        return selftest._RunOutcome(
            produced_bytes=4, eos_seen=True, returned_to_null=True
        )

    def fake_inspect(path):
        return {"duration": 1.0, "streams": [{"type": "video"}]}

    with (
        mock.patch.object(selftest.tempfile, "mkdtemp", fake_mkdtemp),
        mock.patch.object(selftest, "run_pipeline_for", fake_run),
        mock.patch.object(selftest, "inspect_file", fake_inspect),
    ):
        yield


def test_artifacts_are_removed_unless_keep_is_requested(tmp_path: Path) -> None:
    """``--keep`` must actually preserve the produced recordings.

    The harness previously used ``TemporaryDirectory``, which deleted the folder
    when it was garbage collected, so ``--keep`` silently did nothing.
    """
    with _stubbed_media(tmp_path):
        selftest.run_self_test(workdir=None, duration_seconds=0.1, keep_artifacts=True)

    leftovers = list(tmp_path.glob("troika-selftest-*"))
    assert leftovers, "--keep must leave the artifacts on disk"


def test_artifacts_are_removed_by_default(tmp_path: Path) -> None:
    with _stubbed_media(tmp_path):
        selftest.run_self_test(workdir=None, duration_seconds=0.1, keep_artifacts=False)

    assert not list(tmp_path.glob("troika-selftest-*")), (
        "a temporary run must clean up after itself"
    )
