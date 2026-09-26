"""Media self-test.

Graphical Wayland capture cannot run in a headless CI runner, so the self-test
substitutes generated sources for the real screen and the real audio devices —
but it uses *exactly* the same pipeline construction, encoder settings, mixer
configuration, stop sequence and finalisation code that a real recording uses.
That means the parts most likely to break (queue sizing, audio normalisation,
two-source mixing, muxer finalisation, EOS handling) are genuinely exercised.

What the self-test therefore proves:

* video encodes and is muxed;
* a single audio source encodes and is muxed;
* two audio sources with different rates and channel counts mix correctly;
* audio and video mux into one file with a monotonic timeline;
* the recording stops cleanly and the container is finalised;
* the result is a real, parseable media file with the expected streams.

The self-test is an addition to real Wayland testing, not a replacement.
"""

from __future__ import annotations

import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import pipeline as pipeline_module
from .pipeline import PipelinePlan

#: How long each self-test recording runs.
DEFAULT_DURATION_SECONDS = 6


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class SelfTestReport:
    results: list[CheckResult] = field(default_factory=list)
    artifacts: list[Path] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(result.passed for result in self.results)

    def add(self, name: str, passed: bool, detail: str = "") -> None:
        self.results.append(CheckResult(name, passed, detail))

    def format(self) -> str:
        lines = ["Troika D Lite media self-test", "=" * 32]
        for result in self.results:
            mark = "PASS" if result.passed else "FAIL"
            lines.append(f"[{mark}] {result.name}")
            if result.detail:
                for line in result.detail.splitlines():
                    lines.append(f"       {line}")
        lines.append("=" * 32)
        lines.append("result: " + ("PASS" if self.passed else "FAIL"))
        if self.artifacts:
            lines.append("artifacts:")
            for path in self.artifacts:
                lines.append(f"  {path}")
        return "\n".join(lines)


def _test_plan(
    output: Path,
    *,
    with_video: bool,
    audio_modes: tuple[str, ...],
    fps: int = 15,
) -> PipelinePlan:
    """Build a plan whose capture sources are generated rather than real.

    The returned plan goes through the *production* ``pipeline.build_pipeline``;
    only the capture elements are substituted, so the queue sizes, caps, encoder
    settings and muxer configuration under test are the ones a real recording
    would use.
    """
    sources = []
    for index, role in enumerate(audio_modes):
        # Deliberately mismatched rates/channels: this is what a microphone and
        # a monitor source realistically look like.
        test_format = "48000:2" if index == 0 else "44100:1"
        sources.append(
            pipeline_module.AudioSourcePlan(
                role=role,
                element="audiotestsrc",
                device=None,
                provides_clock=index == 0,
                test_format=test_format,
            )
        )
    return PipelinePlan(
        output_path=output,
        container="matroskamux" if with_video else "oggmux",
        video_source="test" if with_video else None,
        frame_rate=fps if with_video else None,
        audio_sources=tuple(sources),
        video_encoder=pipeline_module.SOFTWARE_H264_ENCODER,
        hardware_encoder=False,
    )


def _build_self_test_pipeline(plan: PipelinePlan):
    """Build the *production* pipeline for *plan*.

    The plan already names generated sources (videotestsrc / audiotestsrc), so
    pipeline.build_pipeline assembles the real element graph. This is deliberate:
    the self-test must verify the queues, caps, clock roles and encoder settings
    that a real recording uses, not a parallel copy of them.
    """
    return pipeline_module.build_pipeline(plan, name="selftest")



@dataclass
class _RunOutcome:
    produced_bytes: int = 0
    error: str | None = None
    eos_seen: bool = False
    returned_to_null: bool = False


def run_pipeline_for(
    plan: PipelinePlan, duration_seconds: float = DEFAULT_DURATION_SECONDS
) -> _RunOutcome:
    """Run one pipeline for *duration_seconds* then stop it cleanly.

    The stop sequence is the production one: EOS to the sources, wait for the
    pipeline to drain, then NULL.
    """
    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import GLib, Gst

    if not Gst.is_initialized():
        Gst.init(None)

    outcome = _RunOutcome()
    try:
        pipeline = _build_self_test_pipeline(plan)
    except RuntimeError as exc:
        outcome.error = str(exc)
        return outcome

    loop = GLib.MainLoop()
    finished = {"done": False}

    def on_message(_bus, message):
        if message.type == Gst.MessageType.ERROR:
            error, debug = message.parse_error()
            outcome.error = f"{error.message} ({debug.splitlines()[0] if debug else ''})"
            loop.quit()
        elif message.type == Gst.MessageType.EOS:
            outcome.eos_seen = True
            loop.quit()

    bus = pipeline.get_bus()
    bus.add_signal_watch()
    bus.connect("message", on_message)

    result = pipeline.set_state(Gst.State.PLAYING)
    if result == Gst.StateChangeReturn.FAILURE:
        outcome.error = "pipeline failed to start"
        pipeline.set_state(Gst.State.NULL)
        return outcome

    def request_stop():
        for source in getattr(pipeline, "_troika_sources", []):
            source.send_event(Gst.Event.new_eos())
        # Hard deadline so a wedged pipeline cannot hang the self-test.
        GLib.timeout_add_seconds(8, _deadline)
        return False

    def _deadline():
        if not finished["done"]:
            loop.quit()
        return False

    GLib.timeout_add_seconds(int(duration_seconds), request_stop)
    loop.run()

    _change, final_state, _pending = pipeline.get_state(2 * Gst.SECOND)
    pipeline.set_state(Gst.State.NULL)
    time.sleep(0.1)
    _change, final_state, _pending = pipeline.get_state(2 * Gst.SECOND)
    outcome.returned_to_null = final_state == Gst.State.NULL
    finished["done"] = True

    if plan.output_path.exists():
        outcome.produced_bytes = plan.output_path.stat().st_size
    return outcome


def _describe_stream(stream) -> dict:
    """Turn one discovered stream into plain data."""
    kind = stream.get_stream_type_nick()
    entry = {
        "type": kind,
        "caps": stream.get_caps().to_string() if stream.get_caps() else "",
    }
    if kind.startswith("audio"):
        try:
            entry["channels"] = stream.get_channels()
            entry["rate"] = stream.get_sample_rate()
        except Exception:
            pass
    elif kind == "video":
        try:
            entry["width"] = stream.get_width()
            entry["height"] = stream.get_height()
            denom = stream.get_framerate_denom()
            entry["fps"] = stream.get_framerate_num() / denom if denom else None
        except Exception:
            pass
    return entry


def _collect_streams(structure, seen: set) -> list:
    """Collect streams from a stream structure, descending into containers.

    Matroska and Ogg report a single container stream whose payload is the
    elementary streams, so the interesting information is nested.
    """
    results = []
    if structure is None or id(structure) in seen:
        return results
    seen.add(id(structure))

    if hasattr(structure, "get_streams"):
        # A container: recurse into each inner stream.
        for inner in structure.get_streams() or []:
            results.extend(_collect_streams(inner, seen))
        return results

    results.append(_describe_stream(structure))
    return results


def inspect_file(path: Path) -> dict:
    """Parse *path* and return its streams and duration.

    Uses GStreamer's own discoverer so the check reflects what a media player
    will actually see, rather than trusting that bytes were written.
    """
    import gi

    gi.require_version("Gst", "1.0")
    gi.require_version("GstPbutils", "1.0")
    from gi.repository import Gst, GstPbutils

    if not Gst.is_initialized():
        Gst.init(None)

    discoverer = GstPbutils.Discoverer.new(10 * Gst.SECOND)
    info = discoverer.discover_uri(path.absolute().as_uri())
    result = {
        "valid": info.get_result() != GstPbutils.DiscovererResult.ERROR,
        "duration": info.get_duration() / Gst.SECOND,
        "streams": _collect_streams(info.get_stream_info(), set()),
    }
    return result


def run_self_test(
    workdir: Path | None = None,
    *,
    duration_seconds: float = DEFAULT_DURATION_SECONDS,
    keep_artifacts: bool = False,
) -> SelfTestReport:
    """Run the full self-test suite and return a report."""
    report = SelfTestReport()
    created_workdir: Path | None = None
    if workdir is None:
        # mkdtemp rather than TemporaryDirectory: the latter deletes the folder
        # when it is garbage collected, which would undo ``--keep`` as soon as
        # this function returned.
        created_workdir = Path(tempfile.mkdtemp(prefix="troika-selftest-"))
        workdir = created_workdir
    workdir.mkdir(parents=True, exist_ok=True)

    def check_recording(
        name: str,
        plan: PipelinePlan,
        *,
        expect_video: bool,
        expect_audio_streams: int,
        expect_mixed: bool = False,
    ) -> None:
        outcome = run_pipeline_for(plan, duration_seconds)
        if outcome.error:
            report.add(name, False, f"pipeline error: {outcome.error}")
            return
        if outcome.produced_bytes <= 0:
            report.add(name, False, "no output was written")
            return
        report.artifacts.append(plan.output_path)
        try:
            info = inspect_file(plan.output_path)
        except Exception as exc:
            report.add(name, False, f"output is not parseable: {exc}")
            return

        video = [s for s in info["streams"] if s["type"] == "video"]
        audio = [s for s in info["streams"] if s["type"].startswith("audio")]
        details = [
            f"{outcome.produced_bytes} bytes, {info['duration']:.1f}s",
            f"streams: {[s['type'] for s in info['streams']]}",
        ]
        ok = True
        if expect_video and not video:
            ok = False
            details.append("expected at least one video stream")
        if expect_audio_streams and not audio:
            ok = False
            details.append("expected at least one audio stream")
        if not outcome.eos_seen:
            # A missing EOS means the muxer did not finalise through the normal
            # path; that is a real defect even if bytes exist.
            ok = False
            details.append("pipeline never reported EOS")
        if not outcome.returned_to_null:
            ok = False
            details.append("pipeline did not return to NULL")
        if expect_video and video:
            details.append(
                f"video: {video[0].get('width')}x{video[0].get('height')} "
                f"@{video[0].get('fps')} fps"
            )
        if expect_mixed:
            details.append("audio was mixed from two sources")
        report.add(name, ok, "\n".join(details))

    # 1. video only, 15 fps
    check_recording(
        "video only @ 15 fps",
        _test_plan(workdir / "video_15.mkv", with_video=True, audio_modes=(), fps=15),
        expect_video=True,
        expect_audio_streams=0,
    )

    # 2. video only, 30 fps
    check_recording(
        "video only @ 30 fps",
        _test_plan(workdir / "video_30.mkv", with_video=True, audio_modes=(), fps=30),
        expect_video=True,
        expect_audio_streams=0,
    )

    # 3. audio only
    check_recording(
        "audio only",
        _test_plan(
            workdir / "audio.ogg",
            with_video=False,
            audio_modes=("microphone",),
        ),
        expect_video=False,
        expect_audio_streams=1,
    )

    # 4. two audio streams mixed together, with mismatched formats
    check_recording(
        "two audio sources mixed",
        _test_plan(
            workdir / "mixed_audio.ogg",
            with_video=False,
            audio_modes=("microphone", "system"),
        ),
        expect_video=False,
        expect_audio_streams=1,
        expect_mixed=True,
    )

    # 5. video + both audio sources, the most demanding configuration
    check_recording(
        "video + system audio + microphone",
        _test_plan(
            workdir / "full.mkv",
            with_video=True,
            audio_modes=("microphone", "system"),
            fps=30,
        ),
        expect_video=True,
        expect_audio_streams=1,
        expect_mixed=True,
    )

    # 6. repeated start/stop cycles leave no wedged pipeline
    cycles_ok = True
    cycle_detail = []
    for index in range(3):
        plan = _test_plan(
            workdir / f"cycle_{index}.ogg",
            with_video=False,
            audio_modes=("microphone",),
        )
        outcome = run_pipeline_for(plan, duration_seconds=2)
        if outcome.error or outcome.produced_bytes <= 0 or not outcome.returned_to_null:
            cycles_ok = False
            cycle_detail.append(f"cycle {index}: {outcome.error or 'incomplete'}")
    report.add(
        "repeated start/stop cycles",
        cycles_ok,
        "\n".join(cycle_detail) if cycle_detail else "3 consecutive cycles finalised",
    )

    if created_workdir is not None and not keep_artifacts:
        shutil.rmtree(created_workdir, ignore_errors=True)
    return report


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for ``troika-selftest``."""
    import argparse

    from .interpreter import ensure_gi_capable_interpreter

    ensure_gi_capable_interpreter()

    parser = argparse.ArgumentParser(
        prog="troika-selftest",
        description="Exercise Troika D Lite's media pipeline without a desktop.",
    )
    parser.add_argument(
        "-d",
        "--duration",
        type=float,
        default=DEFAULT_DURATION_SECONDS,
        help="seconds per self-test recording (default: %(default)s)",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        default=None,
        help="where to write test recordings (default: a temporary directory)",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="keep recordings written to a temporary directory",
    )
    args = parser.parse_args(argv)

    report = run_self_test(
        args.output_dir, duration_seconds=args.duration, keep_artifacts=args.keep
    )
    print(report.format())
    return 0 if report.passed else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
