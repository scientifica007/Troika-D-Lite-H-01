"""Optional, low-overhead recording diagnostics.

The instrumentation is deliberately minimal. When diagnostics are disabled — as
they are during normal use — no pad probes are attached at all and nothing is
collected. When enabled, each probe does a counter increment and a subtraction
to detect a timestamp gap; there is no per-frame processing, no formatting and
no terminal output until the recording ends, when a single summary is produced.

The counters answer the question the acceptance tests actually ask: *were there
interruptions?*  A gap count is far more informative than an average, because a
recording can have excellent average throughput and still contain a two-second
freeze.
"""

from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass, field

#: A video timestamp gap larger than this is a visible freeze.
VIDEO_GAP_THRESHOLD_NS = 300_000_000
#: An audio timestamp gap larger than this is an audible dropout.
AUDIO_GAP_THRESHOLD_NS = 250_000_000


@dataclass
class StreamStats:
    """Counters for one recorded stream."""

    name: str
    buffers: int = 0
    first_pts_ns: int | None = None
    last_pts_ns: int | None = None
    max_gap_ns: int = 0
    gaps: list[int] = field(default_factory=list)
    bytes_seen: int = 0

    def observe(self, pts_ns: int | None, size: int) -> None:
        self.buffers += 1
        self.bytes_seen += size
        if pts_ns is None:
            return
        if self.first_pts_ns is None:
            self.first_pts_ns = pts_ns
        elif self.last_pts_ns is not None and pts_ns > self.last_pts_ns:
            gap = pts_ns - self.last_pts_ns
            if gap > self.max_gap_ns:
                self.max_gap_ns = gap
            threshold = (
                VIDEO_GAP_THRESHOLD_NS if self._is_video else AUDIO_GAP_THRESHOLD_NS
            )
            if gap > threshold:
                self.gaps.append(gap)
        # Out-of-order or repeated timestamps are dropped silently: they are
        # normal around stream startup and are not interruptions.
        if self.last_pts_ns is None or pts_ns >= self.last_pts_ns:
            self.last_pts_ns = pts_ns

    _is_video: bool = False

    @property
    def duration_ns(self) -> int:
        if self.first_pts_ns is None or self.last_pts_ns is None:
            return 0
        return max(0, self.last_pts_ns - self.first_pts_ns)

    @property
    def effective_fps(self) -> float | None:
        duration = self.duration_ns
        if duration <= 0 or self.buffers < 2:
            return None
        # One fewer interval than buffers.
        return (self.buffers - 1) / (duration / 1_000_000_000)


class Diagnostics:
    """Collects per-stream statistics and produces an end-of-recording report."""

    def __init__(self, enabled: bool = False) -> None:
        self.enabled = bool(enabled)
        self.streams: dict[str, StreamStats] = {}
        self.messages: Counter[str] = Counter()
        self.notes: dict[str, object] = {}
        self.started_at: float | None = None
        self.stopped_at: float | None = None

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        self.started_at = time.monotonic()
        self.stopped_at = None

    def stop(self) -> None:
        self.stopped_at = time.monotonic()

    @property
    def wall_duration(self) -> float:
        if self.started_at is None:
            return 0.0
        end = self.stopped_at if self.stopped_at is not None else time.monotonic()
        return end - self.started_at

    # -- recording ----------------------------------------------------------

    def note(self, key: str, value: object) -> None:
        self.notes[key] = value

    def note_message(self, text: str) -> None:
        if self.enabled:
            self.messages[text] += 1

    def stream(self, name: str, is_video: bool = False) -> StreamStats:
        stats = self.streams.get(name)
        if stats is None:
            stats = StreamStats(name=name, _is_video=is_video)
            self.streams[name] = stats
        return stats

    def observe(self, name: str, is_video: bool, pts_ns: int | None, size: int) -> None:
        self.stream(name, is_video).observe(pts_ns, size)

    # -- reporting ----------------------------------------------------------

    def attach(self, pipeline) -> None:
        """Attach counting probes to the streams that feed the muxer.

        Does nothing when diagnostics are disabled, which is the point: the
        normal recording path pays nothing for instrumentation it does not use.

        The probes are placed on the last queue of each branch rather than on
        every element. That measures exactly what the container receives — after
        encoding, so the timestamps are the real ones — and avoids counting the
        same frame once per intermediate element.
        """
        if not self.enabled or pipeline is None:
            return

        from gi.repository import Gst

        for name, is_video in (
            ("video_mux_queue", True),
            ("audio_mux_queue", False),
        ):
            element = pipeline.get_by_name(name)
            if element is None:
                continue
            pad = element.get_static_pad("src")
            if pad is None:
                continue
            role = "video" if is_video else "audio"
            pad.add_probe(
                Gst.PadProbeType.BUFFER,
                self._probe,
                role,
                is_video,
            )

    def _probe(self, pad, info, role, is_video):
        from gi.repository import Gst

        buffer = info.get_buffer()
        if buffer is None:
            return Gst.PadProbeReturn.OK
        pts = buffer.pts if buffer.pts != Gst.CLOCK_TIME_NONE else None
        self.observe(role, is_video, pts, buffer.get_size())
        return Gst.PadProbeReturn.OK

    def summary(self) -> dict:
        """Return the collected statistics as plain data."""
        streams = {}
        for name, stats in self.streams.items():
            streams[name] = {
                "buffers": stats.buffers,
                "duration_seconds": round(stats.duration_ns / 1_000_000_000, 3),
                "effective_fps": (
                    round(stats.effective_fps, 2)
                    if stats.effective_fps is not None
                    else None
                ),
                "max_gap_ms": round(stats.max_gap_ns / 1_000_000, 1),
                "gaps_over_threshold": len(stats.gaps),
                "largest_gaps_ms": [
                    round(gap / 1_000_000, 1) for gap in sorted(stats.gaps, reverse=True)[:5]
                ],
            }
        return {
            "wall_seconds": round(self.wall_duration, 3),
            "streams": streams,
            "warnings": dict(self.messages),
            "notes": dict(self.notes),
        }

    def format_summary(self) -> str:
        """Render :meth:`summary` as a concise, human-readable block."""
        data = self.summary()
        lines = [
            "--- Troika D Lite diagnostics ---",
            f"wall clock        : {data['wall_seconds']:.1f} s",
        ]
        for key, value in data["notes"].items():
            lines.append(f"{key:<17} : {value}")
        for name, stats in data["streams"].items():
            fps = f"{stats['effective_fps']:.2f}" if stats["effective_fps"] else "n/a"
            lines.append(
                f"[{name}] buffers={stats['buffers']} "
                f"duration={stats['duration_seconds']:.1f}s "
                f"effective_fps={fps} "
                f"max_gap={stats['max_gap_ms']:.0f}ms "
                f"gaps>{int(VIDEO_GAP_THRESHOLD_NS / 1e6)}ms={stats['gaps_over_threshold']}"
            )
            if stats["largest_gaps_ms"]:
                lines.append(f"    largest gaps (ms): {stats['largest_gaps_ms']}")
        if data["warnings"]:
            lines.append(f"warnings: {data['warnings']}")
        lines.append("--- end diagnostics ---")
        return "\n".join(lines)
