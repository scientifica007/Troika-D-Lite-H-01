"""Tests for the recorder state machine.

These exercise the transitions and the cleanup guarantees without a real
capture session: pipeline construction and the portal are replaced with
inspectable stand-ins, so the assertions are about *the state machine's*
behaviour — does Stop before Start do nothing, is a cancelled startup cleaned
up, is a duplicate Stop safe, does a recording failure still finalise the file?
"""

from __future__ import annotations

from pathlib import Path

import pytest

from troika import portal as portal_module
from troika import recorder as recorder_module
from troika.config import RecordingConfig
from troika.modes import AudioMode, RecordingType
from troika.pipeline import PipelinePlan
from troika.recorder import Recorder, RecorderState


def _state_name(state) -> str:
    """Normalise a ``Gst.State`` value (or a string) to its nickname."""
    if isinstance(state, str):
        return state
    return getattr(state, "value_nick", str(state)).upper()


def _gst_state_change(name: str):
    """Return the real ``Gst.StateChangeReturn`` member named *name*."""
    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst

    return getattr(Gst.StateChangeReturn, name)


# ``set_state`` must return a genuine Gst.StateChangeReturn: its first member
# (FAILURE) has integer value 0, so a plain ``0`` would be read as a failure.
def _success():
    return _gst_state_change("SUCCESS")


class FakePipeline:
    """Stands in for a GStreamer pipeline and records what it is asked to do."""

    def __init__(self):
        self.states = []
        self.sources_eos = 0
        self.bus = FakeBus()
        self._state = "NULL"
        self._troika_sources = None

    def install_sources(self):
        """Populate the source list the recorder sends EOS to."""
        outer = self

        class _Sendable:
            def send_event(self, _event):
                outer.sources_eos += 1

        self._troika_sources = [_Sendable()]

    def set_state(self, state):
        self._state = _state_name(state)
        self.states.append(self._state)
        return _success()

    def get_state(self, _timeout):
        return (_success(), _FakeGstState(self._state), 0)

    def get_bus(self):
        return self.bus

    # A real Gst.Pipeline carries this as a plain attribute, so the fake does too.
    _troika_sources = None

    def make_sources(self):
        outer = self

        class _Sendable:
            def send_event(self, _event):
                outer.sources_eos += 1

        return [_Sendable()]


class _FakeGstState:
    """Minimal stand-in for a ``Gst.State`` enum value."""

    def __init__(self, nick):
        self._nick = nick

    def __eq__(self, other):
        return self._nick in str(other)

    def __hash__(self):
        return hash(self._nick)


class FakeBus:
    def __init__(self):
        self.watch_added = False
        self.watch_removed = False
        self.handlers = []

    def add_signal_watch(self):
        self.watch_added = True

    def remove_signal_watch(self):
        self.watch_removed = True

    def connect(self, _signal, _handler):
        self.handlers.append(_handler)
        return len(self.handlers)


class FakePortalSession:
    """Stands in for :class:`troika.portal.ScreenCastSession`."""

    instances: list["FakePortalSession"] = []

    def __init__(self, timeout_seconds=120):
        self.closed = False
        self.opened = False
        self.on_ready = None
        self.on_error = None
        FakePortalSession.instances.append(self)

    def open(self, on_ready, on_error):
        self.opened = True
        self.on_ready = on_ready
        self.on_error = on_error

    def close(self):
        self.closed = True


class _FakeStream:
    node_id = 42
    properties = {"size": "1920x1080"}
    fd = None

    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def reset_fakes():
    FakePortalSession.instances = []
    yield
    FakePortalSession.instances = []


# -- helpers ------------------------------------------------------------------


def make_plan(config, output_path, video_source=None):
    """A plan with no real capture sources behind it."""
    return PipelinePlan(
        output_path=Path(output_path),
        container="matroskamux",
        video_source=video_source,
        frame_rate=30,
        audio_sources=(),
        video_encoder="x264enc",
        hardware_encoder=False,
    )


@pytest.fixture
def fake_pipeline(monkeypatch):
    """Route ``start`` through a fake pipeline instead of the portal."""
    pipeline = FakePipeline()
    pipeline.install_sources()
    monkeypatch.setattr(
        recorder_module.pipeline_module,
        "build_plan",
        lambda config, output_path, **kw: make_plan(config, output_path),
    )
    monkeypatch.setattr(
        recorder_module.pipeline_module,
        "build_pipeline",
        lambda plan, portal_stream=None: pipeline,
    )
    return pipeline


@pytest.fixture
def fake_portal(monkeypatch):
    """Route ``start`` through the portal path with a fake session."""
    monkeypatch.setattr(
        recorder_module.pipeline_module,
        "build_plan",
        lambda config, output_path, **kw: make_plan(
            config, output_path, video_source="portal"
        ),
    )
    monkeypatch.setattr(
        recorder_module.pipeline_module,
        "build_pipeline",
        lambda plan, portal_stream=None: FakePipeline(),
    )
    monkeypatch.setattr(portal_module, "ScreenCastSession", FakePortalSession)
    return FakePortalSession


@pytest.fixture
def recorder() -> Recorder:
    instance = Recorder(on_state_changed=lambda s: None, prefer_hardware=False)
    instance._errors = []
    instance._finished = []
    instance._on_error = instance._errors.append
    instance._on_finished = instance._finished.append
    return instance


def screen_config(tmp_path: Path, **kwargs) -> RecordingConfig:
    defaults = dict(
        recording_type=RecordingType.SCREEN,
        audio_mode=AudioMode.NONE,
        output_dir=tmp_path,
    )
    defaults.update(kwargs)
    return RecordingConfig(**defaults)


def skip_drain(monkeypatch) -> None:
    """Finalise straight away instead of polling for the drain."""
    monkeypatch.setattr(recorder_module, "_eos", lambda: "eos-event")
    monkeypatch.setattr(
        recorder_module.Recorder, "_schedule_finish", lambda self: self._finalise()
    )


# -- initial state ------------------------------------------------------------


def test_recorder_starts_idle(recorder: Recorder) -> None:
    assert recorder.state is RecorderState.IDLE
    assert recorder.output_path is None


def test_stop_before_start_does_nothing(recorder: Recorder) -> None:
    recorder.stop()
    assert recorder.state is RecorderState.IDLE
    assert recorder._errors == []


# -- successful startup -------------------------------------------------------


def test_start_builds_a_pipeline_and_reports_recording(recorder, tmp_path, fake_pipeline) -> None:
    assert recorder.start(screen_config(tmp_path)) is True

    assert recorder.state is RecorderState.RECORDING
    assert recorder.output_path is not None
    assert recorder.output_path.parent == tmp_path
    assert recorder._errors == []


def test_start_uses_a_timestamped_matroska_file(recorder, tmp_path, fake_pipeline) -> None:
    recorder.start(screen_config(tmp_path))
    assert recorder.output_path.suffix == ".mkv"
    assert recorder.output_path.name.startswith("Troika-D-Lite_")


def test_a_second_recording_never_reuses_the_first_name(
    recorder, tmp_path, fake_pipeline
) -> None:
    recorder.start(screen_config(tmp_path))
    first = recorder.output_path

    other = Recorder(prefer_hardware=False)
    other.start(screen_config(tmp_path))

    assert other.output_path != first


# -- stop ---------------------------------------------------------------------


def test_stop_sends_eos_to_sources_and_returns_to_idle(
    recorder, tmp_path, fake_pipeline, monkeypatch
) -> None:
    skip_drain(monkeypatch)

    recorder.start(screen_config(tmp_path))
    output = recorder.output_path
    recorder.stop()

    assert fake_pipeline.sources_eos >= 1
    assert "NULL" in fake_pipeline.states[-1]
    assert recorder.state is RecorderState.IDLE
    assert recorder._finished == [output]


def test_stop_is_idempotent(recorder, tmp_path, fake_pipeline, monkeypatch) -> None:
    skip_drain(monkeypatch)

    recorder.start(screen_config(tmp_path))
    recorder.stop()
    recorder.stop()  # must be a no-op, not a crash

    assert recorder.state is RecorderState.IDLE
    assert len(recorder._finished) == 1


def test_the_bus_watch_is_removed_on_stop(recorder, tmp_path, fake_pipeline, monkeypatch) -> None:
    skip_drain(monkeypatch)
    recorder.start(screen_config(tmp_path))
    recorder.stop()

    # Tearing down to NULL is what finalises the container; the bus watch is
    # released so the recorder holds no reference to the pipeline.
    assert "NULL" in fake_pipeline.states
    assert fake_pipeline.bus.watch_removed is True


def test_repeated_cycles_leave_the_recorder_reusable(
    recorder, tmp_path, fake_pipeline, monkeypatch
) -> None:
    skip_drain(monkeypatch)

    outputs = []
    for _ in range(5):
        assert recorder.start(screen_config(tmp_path)) is True
        assert recorder.state is RecorderState.RECORDING
        outputs.append(recorder.output_path)
        recorder.stop()
        assert recorder.state is RecorderState.IDLE

    assert len(recorder._finished) == 5
    assert len(set(outputs)) == 5  # every cycle produced its own file


# -- failure paths ------------------------------------------------------------


def test_invalid_configuration_reports_an_error_and_stays_idle(recorder, tmp_path) -> None:
    bad = RecordingConfig(
        recording_type=RecordingType.SCREEN,
        audio_mode=AudioMode.MICROPHONE,
        microphone=None,
        output_dir=tmp_path,
    )
    assert recorder.start(bad) is False
    assert recorder.state is RecorderState.ERROR
    assert recorder._errors


def test_unwritable_output_folder_is_reported(tmp_path) -> None:
    import os

    if os.geteuid() == 0:
        pytest.skip("running as root, so permission checks do not apply")
    readonly = tmp_path / "ro"
    readonly.mkdir()
    readonly.chmod(0o500)
    try:
        instance = Recorder(prefer_hardware=False)
        errors = []
        instance._on_error = errors.append
        assert instance.start(screen_config(readonly)) is False
        assert errors and "writable" in errors[0].lower()
    finally:
        readonly.chmod(0o700)


def test_missing_system_audio_source_is_reported(recorder, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(recorder_module, "default_monitor", lambda *_a: None)
    config = screen_config(tmp_path, audio_mode=AudioMode.SYSTEM)

    assert recorder.start(config) is False
    assert recorder.state is RecorderState.ERROR
    assert any("system audio" in message.lower() for message in recorder._errors)


def test_pipeline_build_failure_is_reported(recorder, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        recorder_module.pipeline_module,
        "build_plan",
        lambda config, output_path, **kw: make_plan(config, output_path),
    )

    def explode(_plan, portal_stream=None):
        raise RuntimeError("GStreamer element 'x264enc' is unavailable.")

    monkeypatch.setattr(recorder_module.pipeline_module, "build_pipeline", explode)

    assert recorder.start(screen_config(tmp_path)) is False
    assert recorder.state is RecorderState.ERROR
    assert any("x264enc" in message for message in recorder._errors)


def test_a_runtime_error_stops_recording_but_keeps_the_file(
    recorder, tmp_path, fake_pipeline
) -> None:
    recorder.start(screen_config(tmp_path))
    output = recorder.output_path

    # Simulate the pipeline reporting a device error mid-recording.
    recorder._handle_pipeline_error("device disconnected")

    assert recorder.state is RecorderState.ERROR
    assert recorder._errors == ["device disconnected"]
    # The partial recording is still reported as written, not silently lost.
    assert recorder._finished == [output]


# -- portal-driven startup ----------------------------------------------------


def test_portal_startup_waits_in_starting_state(recorder, tmp_path, fake_portal) -> None:
    assert recorder.start(screen_config(tmp_path)) is True

    assert recorder.state is RecorderState.STARTING
    assert FakePortalSession.instances and FakePortalSession.instances[0].opened is True


def test_successful_portal_response_starts_recording(recorder, tmp_path, fake_portal) -> None:
    recorder.start(screen_config(tmp_path))
    session = FakePortalSession.instances[0]

    session.on_ready(_FakeStream())

    assert recorder.state is RecorderState.RECORDING


def test_cancelled_portal_reports_an_error_and_returns_to_idle(
    recorder, tmp_path, fake_portal
) -> None:
    recorder.start(screen_config(tmp_path))
    session = FakePortalSession.instances[0]

    session.on_error(portal_module.PortalCancelled("The request was cancelled."))

    assert recorder.state is RecorderState.ERROR
    assert recorder._errors and "cancelled" in recorder._errors[0]
    assert session.closed is True


def test_stop_during_portal_dialog_cancels_cleanly(recorder, tmp_path, fake_portal) -> None:
    recorder.start(screen_config(tmp_path))
    session = FakePortalSession.instances[0]
    assert recorder.state is RecorderState.STARTING

    recorder.stop()

    # Stop is honoured immediately: the dialog is dismissed and the session
    # released rather than being left open until the startup timeout expires.
    assert recorder.state is RecorderState.IDLE
    assert session.closed is True
    assert recorder._session is None


def test_a_stream_granted_after_stop_is_not_recorded(recorder, tmp_path, fake_portal) -> None:
    recorder.start(screen_config(tmp_path))
    session = FakePortalSession.instances[0]
    stream = _FakeStream()

    recorder.stop()
    # The portal answers anyway, a moment too late.
    session.on_ready(stream)

    assert recorder.state is RecorderState.IDLE
    assert recorder._pipeline is None
    assert stream.closed is True


# -- shutdown -----------------------------------------------------------------


def test_shutdown_releases_the_pipeline(recorder, tmp_path, fake_pipeline) -> None:
    recorder.start(screen_config(tmp_path))
    assert recorder.state is RecorderState.RECORDING

    recorder.shutdown()

    assert recorder._pipeline is None
    assert recorder._session is None


def test_shutdown_releases_a_pending_portal_session(recorder, tmp_path, fake_portal) -> None:
    recorder.start(screen_config(tmp_path))
    session = FakePortalSession.instances[0]

    recorder.shutdown()

    assert session.closed is True
    assert recorder._session is None
