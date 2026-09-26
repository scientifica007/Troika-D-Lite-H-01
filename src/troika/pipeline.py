"""GStreamer pipeline construction.

The module is split in two so the interesting decisions can be tested without a
sound server or a display:

``build_plan``
    A pure function turning a :class:`RecordingConfig` into a :class:`PipelinePlan`
    describing which sources, encoders and container to use.

``PipelineRunner``
    Builds real GStreamer elements, drives state transitions, finalises the
    container on stop and reports errors.

Design notes that matter for reliability
----------------------------------------
*Video.*  Frames arrive from ``pipewiresrc`` on the compositor's schedule. A
leaky queue sits immediately after the source so that a slow encoder can never
push back into the compositor, and so video pressure can never reach the audio
branches. When the queue overflows, old *video* frames are discarded — the
timeline keeps moving forward, audio is untouched, and playback stays in sync.
This is the deliberate expression of "continuity over visual quality".

*Audio.*  Audio is never dropped. Each capture device gets its own thread via a
non-leaky queue sized in time (not buffers), because buffer counts mean
different durations at different sample rates. Microphone and monitor audio are
normalised to a common 48 kHz stereo format before being mixed, so sources with
different rates, channel layouts or formats cannot stall each other.

*Clocking.*  Exactly one audio source provides the pipeline clock; the other is
slaved to it with skew-based resampling. That single master clock is what keeps
two independent audio devices, and the video stream, on one timeline.

*Stop.*  EOS is sent to the live *sources*, not to the pipeline. That lets each
source finish the buffer it is working on and push EOS downstream, so the muxer
writes a complete index and the file is playable even if the encoder was
several frames behind.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from .config import RecordingConfig
from .filenames import AUDIO_EXTENSION
from .modes import RecordingType

#: Common audio format every branch is converted to before mixing/encoding.
AUDIO_RATE = 48000
AUDIO_CHANNELS = 2
AUDIO_CAPS = (
    f"audio/x-raw,rate={AUDIO_RATE},channels={AUDIO_CHANNELS},"
    "format=S16LE,layout=interleaved"
)

#: Device read granularity. 40 ms chunks are large enough to absorb scheduler
#: jitter on a slow CPU while staying far below anything a listener would
#: notice in a recording.
AUDIO_LATENCY_US = 40_000
#: Driver-side ring buffer. Generous on purpose: an under-run here is exactly
#: the crackle the brief asks us to avoid.
AUDIO_BUFFER_US = 400_000

#: Raw video frames buffered between the source and the encoder (~0.2 s at
#: 30 fps). Beyond this, old frames are dropped rather than delaying the source.
VIDEO_QUEUE_BUFFERS = 6

#: Extra time the mixer waits for its inputs, so a late audio branch cannot
#: cause the other to be truncated.
MIXER_LATENCY_NS = 200_000_000

#: Geometry of the generated video used by the self-test. Matches a realistic
#: desktop so the encoder is exercised at a comparable cost to a real recording.
VIDEO_WIDTH = 1280
VIDEO_HEIGHT = 720

#: Hardware encoders, tried in order. Any of them may be absent.
HARDWARE_H264_ENCODERS = (
    "vah264enc",
    "nvh264enc",
    "v4l2h264enc",
    "vaapih264enc",
)
#: The software encoder we fall back to. Always present via gstreamer1.0-plugins-ugly.
SOFTWARE_H264_ENCODER = "x264enc"


@dataclass(frozen=True)
class AudioSourcePlan:
    """One audio capture branch."""

    role: str  # "microphone" or "system"
    element: str  # GStreamer element to instantiate
    device: str | None  # device name, or None for the element default
    provides_clock: bool = False
    #: ``"<rate>:<channels>"`` used only when *element* is ``audiotestsrc``, so
    #: the self-test can present the pipeline with two unmatching branches — the
    #: situation a real microphone and monitor source produce.
    test_format: str | None = None

    def test_format_parts(self) -> tuple[int, int]:
        rate, channels = (self.test_format or "48000:2").split(":")
        return int(rate), int(channels)

    @property
    def label(self) -> str:
        return "microphone" if self.role == "microphone" else "system audio"


@dataclass(frozen=True)
class PipelinePlan:
    """A complete, inspectable description of the pipeline to build."""

    output_path: Path
    container: str
    video_source: str | None  # "portal" or "test"
    frame_rate: int | None
    audio_sources: tuple[AudioSourcePlan, ...] = ()
    video_encoder: str = SOFTWARE_H264_ENCODER
    hardware_encoder: bool = False

    @property
    def has_video(self) -> bool:
        return self.video_source is not None

    @property
    def has_audio(self) -> bool:
        return bool(self.audio_sources)

    @property
    def audio_is_mixed(self) -> bool:
        return len(self.audio_sources) > 1

    @property
    def audio_encoder(self) -> str:
        return "opusenc"


def detect_hardware_encoder() -> str | None:
    """Return a usable hardware H.264 encoder name, or ``None``.

    Presence of a GStreamer element is not enough: a VA-API or V4L2 encoder
    element exists on plenty of machines with no usable device. Requiring
    ``/dev/dri`` avoids selecting one that would fail at runtime.
    """
    if not os.path.isdir("/dev/dri"):
        return None
    try:
        import gi

        gi.require_version("Gst", "1.0")
        from gi.repository import Gst
    except (ImportError, ValueError):
        return None
    if not Gst.is_initialized():
        Gst.init(None)
    for name in HARDWARE_H264_ENCODERS:
        if Gst.ElementFactory.find(name) is not None:
            return name
    return None


def build_plan(
    config: RecordingConfig,
    output_path: Path,
    *,
    system_audio_device: str | None = None,
    use_test_sources: bool = False,
    prefer_hardware: bool = True,
) -> PipelinePlan:
    """Translate a validated configuration into a pipeline plan.

    *system_audio_device* is the monitor source chosen for system audio; when
    system audio is requested but no monitor exists the caller should raise
    before reaching here, but a ``None`` value is still handled by letting
    ``pulsesrc`` pick the default monitor.

    *use_test_sources* replaces the real capture sources with generated ones.
    The self-test uses it to exercise the same encoder, muxer and stop logic on
    a machine with no desktop session.
    """
    config.validate()

    if config.recording_type is RecordingType.AUDIO:
        container = "oggmux"
        extension = AUDIO_EXTENSION
    else:
        container = "matroskamux"
        extension = "mkv"

    video_source: str | None = None
    frame_rate: int | None = None
    if config.records_video:
        video_source = "test" if use_test_sources else "portal"
        frame_rate = int(config.frame_rate.value)

    audio_sources: list[AudioSourcePlan] = []
    if config.recording_type is RecordingType.AUDIO:
        audio_sources.append(
            AudioSourcePlan(role="microphone", element="pulsesrc", device=config.microphone)
        )
    elif config.audio_mode.wants_audio:
        if config.wants_microphone:
            audio_sources.append(
                AudioSourcePlan(
                    role="microphone", element="pulsesrc", device=config.microphone
                )
            )
        if config.wants_system_audio:
            audio_sources.append(
                AudioSourcePlan(
                    role="system",
                    element="pulsesrc",
                    device=system_audio_device,
                    provides_clock=_system_only(audio_sources),
                )
            )

    # Exactly one branch may own the pipeline clock. The microphone is
    # preferred because it is usually the steadier device.
    _assign_clock_provider(audio_sources)

    hardware_name = detect_hardware_encoder() if prefer_hardware else None
    if hardware_name is not None:
        video_encoder, is_hardware = hardware_name, True
    else:
        video_encoder, is_hardware = SOFTWARE_H264_ENCODER, False

    if use_test_sources:
        # The generated sources are not a hardware-encoder validation.
        video_encoder, is_hardware = SOFTWARE_H264_ENCODER, False

    output_path = Path(output_path)
    if output_path.suffix != f".{extension}":
        output_path = output_path.with_suffix(f".{extension}")

    return PipelinePlan(
        output_path=output_path,
        container=container,
        video_source=video_source,
        frame_rate=frame_rate,
        audio_sources=tuple(audio_sources),
        video_encoder=video_encoder,
        hardware_encoder=is_hardware,
    )


def _system_only(existing: list[AudioSourcePlan]) -> bool:
    """System audio provides the clock only when no microphone is present."""
    return not any(source.role == "microphone" for source in existing)


def _assign_clock_provider(audio_sources: list[AudioSourcePlan]) -> None:
    """Mark the clock-providing branch, preferring the microphone."""
    order = {"microphone": 0, "system": 1}
    ranked = sorted(audio_sources, key=lambda s: order.get(s.role, 2))
    for index, source in enumerate(ranked):
        audio_sources[audio_sources.index(source)] = AudioSourcePlan(
            role=source.role,
            element=source.element,
            device=source.device,
            provides_clock=index == 0,
        )


# ---------------------------------------------------------------------------
# Element construction
# ---------------------------------------------------------------------------


def _require_gst():
    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst

    if not Gst.is_initialized():
        Gst.init(None)
    return Gst


def _make(Gst, factory: str, name: str | None = None):
    element = Gst.ElementFactory.make(factory, name)
    if element is None:
        raise RuntimeError(
            f"GStreamer element '{factory}' is unavailable. "
            "Install the matching gstreamer1.0-plugins-* package."
        )
    return element


def _configure_video_encoder(Gst, encoder, plan: PipelinePlan) -> None:
    """Apply encoder settings appropriate for an older CPU."""
    fps = plan.frame_rate or 30
    if plan.video_encoder == "x264enc":
        # veryfast + zerolatency keeps per-frame cost low; zerolatency also
        # disables B-frames and lookahead, which is what makes it safe to stop
        # the recording at any moment.
        encoder.set_property("speed-preset", "veryfast")
        encoder.set_property("tune", "zerolatency")
        encoder.set_property("key-int-max", fps * 2)
        encoder.set_property("bitrate", 4000 if fps >= 30 else 2500)
        encoder.set_property("threads", 0)
    elif plan.video_encoder == "v4l2h264enc":
        encoder.set_property("extra-controls", None)
    else:
        # Hardware encoders differ too much to configure generically; their
        # defaults are used and the software path is the documented fallback.
        pass


def _configure_audio_source(Gst, source, plan_source: AudioSourcePlan) -> None:
    """Apply buffer sizing and clock roles to a capture source.

    Works for ``pulsesrc`` and for ``audiotestsrc``: the self-test runs the same
    code path, with generated audio standing in for a real device.
    """
    if plan_source.element == "audiotestsrc":
        source.set_property("is-live", True)
        source.set_property("wave", "sine")
        # Distinct tones make a mixed track audibly verifiable.
        source.set_property(
            "freq", 440.0 if plan_source.role == "microphone" else 660.0
        )
        rate, channels = plan_source.test_format_parts()
        caps = _make(Gst, "capsfilter")
        caps.set_property(
            "caps",
            Gst.Caps.from_string(
                f"audio/x-raw,rate={rate},channels={channels},format=S16LE"
            ),
        )
        return caps

    if source.find_property("buffer-time") is not None:
        source.set_property("buffer-time", AUDIO_BUFFER_US)
    if source.find_property("latency-time") is not None:
        source.set_property("latency-time", AUDIO_LATENCY_US)
    if source.find_property("provide-clock") is not None:
        source.set_property("provide-clock", plan_source.provides_clock)
    if source.find_property("slave-method") is not None and not plan_source.provides_clock:
        # Skew-correct the non-master device against the pipeline clock rather
        # than letting it free-run. This is what prevents two sound devices
        # from drifting apart over a long recording.
        source.set_property("slave-method", "skew")
    if plan_source.device and source.find_property("device") is not None:
        source.set_property("device", plan_source.device)
    source.set_property("do-timestamp", True)
    # A recording source should never be allowed to silently stall the whole
    # pipeline; pulsesrc reports a device failure instead.
    if source.find_property("automatic-eos") is not None:
        source.set_property("automatic-eos", False)
    return None


def _add_video_branch(
    Gst, pipeline, plan: PipelinePlan, portal_stream
) -> list:
    """Build the video branch, returning the source elements for EOS delivery."""
    extra_elements: list = []
    if plan.video_source == "test":
        source = _make(Gst, "videotestsrc", "video_src")
        source.set_property("is-live", True)
        source.set_property("pattern", "smpte")
        # videotestsrc has no width/height properties, so the frame geometry is
        # pinned with a capsfilter directly after it. The real capture path must
        # never do this: forcing a size there would rescale or fail negotiation.
        geometry = _make(Gst, "capsfilter", "test_geometry_caps")
        geometry.set_property(
            "caps",
            Gst.Caps.from_string(
                f"video/x-raw,width={VIDEO_WIDTH},height={VIDEO_HEIGHT}"
            ),
        )
        extra_elements.append(geometry)
    else:
        source = _make(Gst, "pipewiresrc", "video_src")
        if portal_stream is None:
            raise RuntimeError("Screen capture requires an active portal stream.")
        # pipewiresrc takes ownership of the descriptor it is given; duplicating
        # it keeps the portal session's own fd independent and correct to close.
        fd = os.dup(portal_stream.fd) if portal_stream.fd is not None else -1
        source.set_property("fd", fd)
        source.set_property("path", str(portal_stream.node_id))
        source.set_property("do-timestamp", True)
        source.set_property("automatic-eos", False)
        if source.find_property("keepalive-time") is not None:
            source.set_property("keepalive-time", 1000)

    convert = _make(Gst, "videoconvert")
    rate = _make(Gst, "videorate")
    caps = _make(Gst, "capsfilter", "video_rate_caps")
    # Pin I420 as well as the frame rate. x264 wants 4:2:0 internally, so asking
    # for it here moves the conversion into videoconvert (which does one pass)
    # instead of letting it happen implicitly inside the encoder; it also keeps
    # the output at standard 4:2:0 High Profile rather than a 4:2:2 variant that
    # some players handle poorly. videoconvert sits upstream, so negotiation
    # makes it produce I420 for us.
    caps.set_property(
        "caps",
        Gst.Caps.from_string(
            f"video/x-raw,format=I420,framerate={plan.frame_rate}/1"
        ),
    )
    # Decouple the compositor's delivery thread from the encoder, and drop old
    # video frames rather than delaying the source.
    queue = _make(Gst, "queue", "video_queue")
    queue.set_property("max-size-buffers", VIDEO_QUEUE_BUFFERS)
    queue.set_property("max-size-bytes", 0)
    queue.set_property("max-size-time", 0)
    queue.set_property("leaky", 2)  # downstream: drop oldest

    encoder = _make(Gst, plan.video_encoder, "video_encoder")
    _configure_video_encoder(Gst, encoder, plan)

    parse = _make(Gst, "h264parse", "video_parse")
    parse.set_property("config-interval", -1)

    # Second queue: absorbs a momentary stall in the muxer without stalling the
    # encoder thread.
    mux_queue = _make(Gst, "queue", "video_mux_queue")
    mux_queue.set_property("max-size-buffers", VIDEO_QUEUE_BUFFERS)
    mux_queue.set_property("max-size-bytes", 0)
    mux_queue.set_property("max-size-time", 0)
    mux_queue.set_property("leaky", 2)

    chain = [source, *extra_elements, convert, rate, caps, queue, encoder, parse, mux_queue]
    for element in chain:
        pipeline.add(element)
    for first, second in zip(chain, chain[1:]):
        if not first.link(second):
            raise RuntimeError(
                f"Cannot link {first.get_name()} to {second.get_name()} "
                "(incompatible video formats)."
            )
    return [source], mux_queue


def _add_audio_branch(Gst, pipeline, plan: PipelinePlan) -> tuple[list, object]:
    """Build the audio branch (one or two sources), returning sources and the
    element whose source pad carries the encoded audio."""
    sources = []
    branches = []
    for plan_source in plan.audio_sources:
        source = _make(Gst, plan_source.element, f"audio_{plan_source.role}")
        source_caps = _configure_audio_source(Gst, source, plan_source)
        convert = _make(Gst, "audioconvert")
        resample = _make(Gst, "audioresample")
        caps = _make(Gst, "capsfilter")
        caps.set_property("caps", Gst.Caps.from_string(AUDIO_CAPS))
        # Time-sized, non-leaky: audio is never discarded. 2 s of slack absorbs
        # a scheduling hiccup or a slow first encoder frame.
        queue = _make(Gst, "queue", f"audio_{plan_source.role}_queue")
        queue.set_property("max-size-buffers", 0)
        queue.set_property("max-size-bytes", 0)
        queue.set_property("max-size-time", 2 * 1_000_000_000)
        queue.set_property("leaky", 0)  # never leak audio

        chain = [source]
        if source_caps is not None:
            # Generated test audio is given a device-like format here, so the
            # normalisation path below still has real work to do.
            chain.append(source_caps)
        chain += [convert, resample, caps, queue]
        for element in chain:
            pipeline.add(element)
        for first, second in zip(chain, chain[1:]):
            if not first.link(second):
                raise RuntimeError(
                    f"Cannot link {first.get_name()} to {second.get_name()} "
                    "(incompatible audio formats)."
                )
        sources.append(source)
        branches.append(queue)

    if len(branches) == 1:
        mixer_output = branches[0]
    else:
        mixer = _make(Gst, "audiomixer", "audio_mixer")
        mixer.set_property("latency", MIXER_LATENCY_NS)
        # Every branch is already normalised, so the mixer only has to sum.
        mixer.set_property("min-upstream-latency", AUDIO_BUFFER_US * 1000)
        pipeline.add(mixer)
        for queue in branches:
            if not queue.link(mixer):
                raise RuntimeError("Cannot link an audio branch to the mixer.")
        # audiomixer negotiates its output channel count from downstream, and
        # with nothing asking for stereo it collapses the mix to mono. Pin the
        # mixed format explicitly so a two-source recording keeps a stereo track.
        mix_convert = _make(Gst, "audioconvert")
        mix_caps = _make(Gst, "capsfilter", "mixed_audio_caps")
        mix_caps.set_property("caps", Gst.Caps.from_string(AUDIO_CAPS))
        pipeline.add(mix_convert)
        pipeline.add(mix_caps)
        if not mixer.link(mix_convert) or not mix_convert.link(mix_caps):
            raise RuntimeError("Cannot link the audio mixer to its output format.")
        mixer_output = mix_caps

    encoder = _make(Gst, "opusenc", "audio_encoder")
    encoder.set_property("bitrate", 128_000 if not plan.has_video else 96_000)
    encoder.set_property("audio-type", "generic")
    encoder.set_property("frame-size", 20)

    mux_queue = _make(Gst, "queue", "audio_mux_queue")
    mux_queue.set_property("max-size-buffers", 0)
    mux_queue.set_property("max-size-bytes", 0)
    mux_queue.set_property("max-size-time", 2 * 1_000_000_000)
    mux_queue.set_property("leaky", 0)

    pipeline.add(encoder)
    pipeline.add(mux_queue)
    if not mixer_output.link(encoder):
        raise RuntimeError("Cannot link the audio mixer to the encoder.")
    if not encoder.link(mux_queue):
        raise RuntimeError("Cannot link the audio encoder to its queue.")
    return sources, mux_queue


def _add_muxer(Gst, pipeline, plan: PipelinePlan, video_tail, audio_tail) -> object:
    muxer = _make(Gst, plan.container, "muxer")
    if plan.container == "matroskamux":
        # Start the earliest stream at zero so the file's timeline begins at 0
        # no matter which branch produced the first buffer.
        muxer.set_property("offset-to-zero", True)
        muxer.set_property("writing-app", "Troika D Lite")
    sink = _make(Gst, "filesink", "file_sink")
    sink.set_property("location", str(plan.output_path))
    sink.set_property("sync", False)
    sink.set_property("async", False)

    pipeline.add(muxer)
    pipeline.add(sink)
    if not muxer.link(sink):
        raise RuntimeError("Cannot link the muxer to the output file.")

    if video_tail is not None and not video_tail.link(muxer):
        raise RuntimeError("Cannot link the video branch to the muxer.")
    if audio_tail is not None and not audio_tail.link(muxer):
        raise RuntimeError("Cannot link the audio branch to the muxer.")
    if video_tail is None and audio_tail is None:
        raise RuntimeError("A pipeline with no streams cannot be recorded.")
    return muxer


def build_pipeline(plan: PipelinePlan, portal_stream=None, name: str = "troika"):
    """Build a GStreamer pipeline for *plan*.

    Raises :class:`RuntimeError` with a user-facing message if any element is
    missing or a link fails, so the caller can report a useful error rather
    than a stack trace.
    """
    Gst = _require_gst()
    pipeline = Gst.Pipeline.new(name)
    video_sources: list = []
    video_tail = None
    audio_sources: list = []
    audio_tail = None

    if plan.has_video:
        video_sources, video_tail = _add_video_branch(Gst, pipeline, plan, portal_stream)
    if plan.has_audio:
        audio_sources, audio_tail = _add_audio_branch(Gst, pipeline, plan)

    _add_muxer(Gst, pipeline, plan, video_tail, audio_tail)

    pipeline._troika_sources = video_sources + audio_sources  # type: ignore[attr-defined]
    pipeline._troika_plan = plan  # type: ignore[attr-defined]
    return pipeline


def describe_plan(plan: PipelinePlan) -> dict:
    """Return a human-readable summary of a plan, used by diagnostics and the
    self-test report."""
    return {
        "output": str(plan.output_path),
        "container": plan.container,
        "video_source": plan.video_source,
        "frame_rate": plan.frame_rate,
        "video_encoder": plan.video_encoder,
        "hardware_encoder": plan.hardware_encoder,
        "audio_sources": [
            {
                "role": source.role,
                "element": source.element,
                "device": source.device,
                "clock_provider": source.provides_clock,
            }
            for source in plan.audio_sources
        ],
        "audio_mixed": plan.audio_is_mixed,
        "audio_caps": AUDIO_CAPS,
        "audio_encoder": plan.audio_encoder if plan.has_audio else None,
    }
