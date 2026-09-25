"""The recording state machine.

This module owns the awkward part of the product: getting from *idle* to
*recording* and reliably back to *idle* again, every time, without leaking a
pipeline or leaving a truncated file behind.

States are explicit and transitions are checked, so an illegal request (Stop
before Start, Start twice) is a no-op rather than a crash. Startup is
asynchronous because the Wayland permission dialog is: while waiting, the state
is ``STARTING`` and a Stop request is honoured by cancelling the pending
startup rather than being ignored.
"""

from __future__ import annotations

import os
from enum import Enum
from pathlib import Path
from typing import Callable

from . import pipeline as pipeline_module
from .config import ConfigError, RecordingConfig
from .devices import default_monitor
from .diagnostics import Diagnostics
from .filenames import (
    check_free_space,
    ensure_writable_directory,
    reserve_output_path,
)
from .modes import RecordingType

#: How long to wait for the encoders to drain after EOS before forcing the
#: stop. Generous enough for a slow software encoder to finish its last frames,
#: short enough that a wedged device cannot hang the application.
DRAIN_TIMEOUT_SECONDS = 15.0
#: How often the drain is checked. This is not a busy loop: it runs only while
#: a recording is being stopped.
DRAIN_POLL_MS = 200


class RecorderState(str, Enum):
    IDLE = "idle"
    STARTING = "starting"
    RECORDING = "recording"
    STOPPING = "stopping"
    ERROR = "error"


class RecorderError(RuntimeError):
    """Raised for user-facing recording failures."""


class Recorder:
    """Drives one recording at a time.

    Callbacks are invoked from the GLib main loop and are all optional:

    ``on_state_changed(state)``      state transitions
    ``on_error(message)``            a failure the user should see
    ``on_finished(path)``            a recording was written successfully
    """

    def __init__(
        self,
        *,
        on_state_changed: Callable[[RecorderState], None] | None = None,
        on_error: Callable[[str], None] | None = None,
        on_finished: Callable[[Path], None] | None = None,
        diagnostics_enabled: bool = False,
        prefer_hardware: bool = True,
    ) -> None:
        self._state = RecorderState.IDLE
        self._config: RecordingConfig | None = None
        self._pipeline = None
        self._session = None  # portal.ScreenCastSession
        self._diagnostics = Diagnostics(enabled=diagnostics_enabled)
        self._prefer_hardware = prefer_hardware
        self._output_path: Path | None = None
        self._pending_stop = False
        self._finish_timeout = None
        self._stop_deadline = 0.0
        self._wall_clock_span_us = 0

        self._on_state_changed = on_state_changed
        self._on_error = on_error
        self._on_finished = on_finished

    # -- observable state ---------------------------------------------------

    @property
    def state(self) -> RecorderState:
        return self._state

    @property
    def output_path(self) -> Path | None:
        return self._output_path

    @property
    def diagnostics(self) -> Diagnostics:
        return self._diagnostics

    @property
    def elapsed_seconds(self) -> float:
        return self._diagnostics.wall_duration

    def _set_state(self, state: RecorderState) -> None:
        if state == self._state:
            return
        self._state = state
        if self._on_state_changed is not None:
            self._on_state_changed(state)

    # -- start --------------------------------------------------------------

    def start(self, config: RecordingConfig) -> bool:
        """Begin recording. Returns ``True`` if startup was initiated.

        Returns ``False`` (and reports via ``on_error``) if the configuration or
        the environment makes recording impossible; the recorder stays idle.
        """
        if self._state not in (RecorderState.IDLE, RecorderState.ERROR):
            return False
        self._set_state(RecorderState.STARTING)
        self._pending_stop = False
        self._output_path = None

        try:
            config.validate()
            ensure_writable_directory(config.output_dir)
            check_free_space(config.output_dir)
            system_audio_device = self._resolve_system_audio(config)
            # Claim the name now, so a second recording started in the same
            # second cannot compute the same path and overwrite this one.
            output_path = reserve_output_path(config.output_dir, config.recording_type)
            plan = pipeline_module.build_plan(
                config,
                output_path,
                system_audio_device=system_audio_device,
                prefer_hardware=self._prefer_hardware,
            )
            self._record_plan_notes(plan)
        except (ConfigError, RuntimeError) as exc:
            self._fail(str(exc))
            return False
        except Exception as exc:  # pragma: no cover - defensive
            self._fail(f"Could not prepare the recording: {exc}")
            return False

        self._config = config
        self._plan = plan
        self._diagnostics = Diagnostics(enabled=self._diagnostics.enabled)
        self._diagnostics.start()

        if plan.video_source == "portal":
            return self._start_portal(plan)
        return self._start_pipeline(plan, portal_stream=None)

    def _resolve_system_audio(self, config: RecordingConfig) -> str | None:
        """Find a monitor source for system audio, or explain why we cannot."""
        if not config.wants_system_audio:
            return None
        monitor = default_monitor()
        if monitor is None:
            raise ConfigError(
                "No system audio source is available. System audio needs a "
                "PipeWire or PulseAudio monitor source on the default output. "
                "Use 'Refresh devices' after starting playback, or choose a "
                "different audio option."
            )
        return monitor.name

    def _record_plan_notes(self, plan) -> None:
        self._diagnostics.note("output file", plan.output_path)
        self._diagnostics.note("container", plan.container)
        self._diagnostics.note("video encoder", plan.video_encoder)
        self._diagnostics.note("hardware encoder", plan.hardware_encoder)
        if plan.frame_rate:
            self._diagnostics.note("requested fps", plan.frame_rate)
        for source in plan.audio_sources:
            self._diagnostics.note(
                f"{source.label} device", source.device or "(server default)"
            )
            self._diagnostics.note(
                f"{source.label} clock", "provides clock" if source.provides_clock else "slaved"
            )

    def _start_portal(self, plan) -> bool:
        from .portal import ScreenCastSession

        self._session = ScreenCastSession()
        self._session.open(
            on_ready=lambda stream: self._on_portal_ready(stream),
            on_error=self._on_portal_error,
        )
        return True

    def _on_portal_ready(self, stream) -> None:
        if self._pending_stop or self._state != RecorderState.STARTING:
            # Stop was pressed while the permission dialog was open.
            stream.close()
            self._reset_after_abort()
            return
        self._diagnostics.note("screen node id", stream.node_id)
        try:
            self._diagnostics.note(
                "screen source props",
                {str(k): str(v) for k, v in stream.properties.items()},
            )
        except Exception:
            pass
        if not self._start_pipeline(self._plan, portal_stream=stream):
            self._session.close()
            self._session = None

    def _on_portal_error(self, error: Exception) -> None:
        if self._pending_stop:
            self._reset_after_abort()
            return
        # Keep the session reference so ``_fail`` closes it, rather than
        # dropping it here and leaking the portal session.
        self._fail(str(error))

    def _start_pipeline(self, plan, portal_stream) -> bool:
        try:
            built = pipeline_module.build_pipeline(plan, portal_stream=portal_stream)
        except RuntimeError as exc:
            self._fail(str(exc))
            return False
        except Exception as exc:  # pragma: no cover - defensive
            self._fail(f"Could not build the recording pipeline: {exc}")
            return False

        self._pipeline = built
        self._output_path = plan.output_path
        self._diagnostics.attach(built)

        bus = built.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_bus_message)

        result = built.set_state(pipeline_module._require_gst().State.PLAYING)
        if result == pipeline_module._require_gst().StateChangeReturn.FAILURE:
            self._fail("The recording pipeline failed to start.")
            return False

        self._set_state(RecorderState.RECORDING)
        return True

    # -- stop ---------------------------------------------------------------

    def stop(self) -> None:
        """Request a clean stop. Safe to call in any state."""
        if self._state in (RecorderState.IDLE, RecorderState.ERROR):
            return
        if self._state == RecorderState.STARTING:
            # The permission dialog is still open. Closing the session asks the
            # portal to dismiss that dialog and releases anything already
            # granted, so Stop is honoured immediately instead of leaving the
            # user waiting for the startup timeout to expire.
            self._pending_stop = True
            self._abort_startup()
            return
        if self._state != RecorderState.RECORDING:
            return

        self._set_state(RecorderState.STOPPING)
        self._diagnostics.stop()

        pipeline = self._pipeline
        if pipeline is None:
            self._finalise()
            return

        # Send EOS to the live sources. They finish their current buffer, push
        # EOS downstream, and the muxer finalises the container. Sending EOS to
        # the pipeline instead would tear it down before the encoder drained.
        for source in getattr(pipeline, "_troika_sources", []):
            try:
                source.send_event(_eos())
            except Exception:
                pass

        self._schedule_finish()

    def _schedule_finish(self) -> None:
        """Wait for the drain, then finalise.

        Without a GLib main loop there is nothing to wait on, so the pipeline is
        finalised straight away rather than being left in ``STOPPING`` forever.
        """
        try:
            from gi.repository import GLib
        except ImportError:  # pragma: no cover - GLib is a hard dependency in practice
            self._finalise()
            return

        import time

        self._stop_deadline = time.monotonic() + DRAIN_TIMEOUT_SECONDS
        self._finish_timeout = GLib.timeout_add(DRAIN_POLL_MS, self._poll_drained)

    def _poll_drained(self) -> bool:
        """Wait for the pipeline to reach EOS, or force the stop eventually."""
        import time

        pipeline = self._pipeline
        if pipeline is None:
            self._finalise()
            return False
        Gst = pipeline_module._require_gst()
        _change, state, _pending = pipeline.get_state(0)
        if state in (Gst.State.PLAYING, Gst.State.PAUSED):
            # Still draining. A live source that was stopped mid-buffer keeps
            # the pipeline in PLAYING until it delivers EOS; the wall-clock
            # deadline guarantees we never hang on a wedged device.
            if time.monotonic() < self._stop_deadline:
                return True
        self._finish_timeout = None
        self._finalise()
        return False

    def _finalise(self) -> None:
        """Tear the pipeline down to NULL and report the output file."""
        pipeline = self._pipeline
        self._pipeline = None
        if pipeline is not None:
            try:
                pipeline.set_state(pipeline_module._require_gst().State.NULL)
            except Exception:
                pass
            try:
                bus = pipeline.get_bus()
                if bus is not None:
                    bus.remove_signal_watch()
            except Exception:
                pass

        session = self._session
        self._session = None
        if session is not None:
            try:
                session.close()
            except Exception:
                pass

        output = self._output_path
        self._output_path = None
        self._config = None
        self._pending_stop = False
        self._set_state(RecorderState.IDLE)

        if output is not None and self._on_finished is not None:
            self._on_finished(output)

    def _abort_startup(self) -> None:
        """Cancel an in-flight portal negotiation and return to idle.

        The session is closed here rather than in the portal callbacks: closing
        it dismisses the permission dialog, and dropping the callbacks means a
        late response cannot start a recording the user already cancelled.
        """
        session = self._session
        self._session = None
        if session is not None:
            try:
                session.close()
            except Exception:
                pass
        self._reset_after_abort()

    def _reset_after_abort(self) -> None:
        """Return to idle after a cancelled or aborted startup."""
        session = self._session
        self._session = None
        if session is not None:
            try:
                session.close()
            except Exception:
                pass
        self._pipeline = None
        self._output_path = None
        self._config = None
        self._pending_stop = False
        self._set_state(RecorderState.IDLE)

    # -- bus messages -------------------------------------------------------

    def _on_bus_message(self, _bus, message) -> None:
        from gi.repository import Gst

        if message.type == Gst.MessageType.ERROR:
            error, debug = message.parse_error()
            text = error.message
            if debug:
                text = f"{text} ({debug.splitlines()[0]})"
            self._handle_pipeline_error(text)
        elif message.type == Gst.MessageType.EOS:
            # The pipeline drained after a stop request: finish immediately
            # rather than waiting for the poll deadline.
            if self._state == RecorderState.STOPPING:
                self._cancel_finish_timeout()
                self._finalise()
        elif message.type == Gst.MessageType.WARNING:
            warning, _debug = message.parse_warning()
            self._diagnostics.note_message(f"warning: {warning.message}")
        elif message.type == Gst.MessageType.QOS:
            if self._diagnostics.enabled:
                self._diagnostics.note_message("qos event")

    def _handle_pipeline_error(self, text: str) -> None:
        """A pipeline error during recording: finalise the file, then report.

        The partial recording is deliberately still reported as written, so a
        device failure cannot silently lose several minutes of video.
        """
        self._diagnostics.note_message(text)
        already_failed = self._state in (RecorderState.IDLE, RecorderState.ERROR)
        if already_failed:
            return
        self._fail(text, during_recording=self._state == RecorderState.RECORDING)

    def _cancel_finish_timeout(self) -> None:
        if self._finish_timeout is None:
            return
        try:
            from gi.repository import GLib

            GLib.source_remove(self._finish_timeout)
        except Exception:
            pass
        self._finish_timeout = None

    def _fail(self, message: str, during_recording: bool = False) -> None:
        self._diagnostics.stop()
        self._cancel_finish_timeout()
        partial = self._output_path if during_recording else None
        if during_recording:
            # Tear down first so the encoder/muxer finalises what it already
            # wrote; the partial recording is still playable.
            self._force_teardown()
        else:
            self._reset_after_abort()
        self._set_state(RecorderState.ERROR)
        if self._on_error is not None:
            self._on_error(message)
        # A recording that failed mid-flight is not lost: report the file that
        # was still finalised so the user can find it.
        if partial is not None and self._on_finished is not None:
            self._on_finished(partial)

    def _force_teardown(self) -> None:
        pipeline = self._pipeline
        self._pipeline = None
        if pipeline is not None:
            try:
                pipeline.set_state(pipeline_module._require_gst().State.NULL)
            except Exception:
                pass
        session = self._session
        self._session = None
        if session is not None:
            try:
                session.close()
            except Exception:
                pass
        self._output_path = None

    # -- shutdown -----------------------------------------------------------

    def shutdown(self) -> None:
        """Release everything. Called when the window closes."""
        if self._state in (RecorderState.STARTING, RecorderState.RECORDING):
            self.stop()
        self._cancel_finish_timeout()
        self._force_teardown()

    def diagnostic_summary(self) -> str:
        return self._diagnostics.format_summary()


def _eos():
    from gi.repository import Gst

    return Gst.Event.new_eos()
