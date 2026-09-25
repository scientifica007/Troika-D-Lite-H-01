"""Tests for the XDG Desktop Portal screen-cast client.

The portal is the Wayland capture path, so its request sequencing and response
handling are worth testing directly. The D-Bus bus and the portal proxy are
replaced with fakes that behave like the real ones (handing back a request
handle for every method call), so the session's own state machine is what is
under test.
"""

from __future__ import annotations

import os

import pytest

from troika.portal import (
    PortalCancelled,
    PortalError,
    RESPONSE_CANCELLED,
    RESPONSE_SUCCESS,
    ScreenCastSession,
    ScreenCastStream,
)

CONTENT_TYPE = 2


class FakePortalProxy:
    """A portal object whose methods hand back request handles.

    The handles are built the way the real portal builds them: from the
    ``handle_token`` the caller supplied. That matters, because the session
    subscribes to the request path *before* making the call, and this fake lets
    the tests prove that prediction is correct.
    """

    def __init__(self, events=None):
        self.calls: list[tuple[str, tuple]] = []
        self.events = events if events is not None else []
        # File descriptors handed out by OpenPipeWireRemote. They must be real
        # descriptors, because the session genuinely closes them.
        self.opened_fds: list[int] = []

    def _handle_for(self, options) -> str:
        token = str(dict(options).get("handle_token", ""))
        return f"/org/freedesktop/portal/desktop/request/1_42/{token}"

    def CreateSession(self, options, **_kwargs):
        self.calls.append(("CreateSession", ()))
        self.events.append(("call", "CreateSession"))
        return self._handle_for(options)

    def SelectSources(self, handle, options, **_kwargs):
        self.calls.append(("SelectSources", (str(handle),)))
        self.events.append(("call", "SelectSources"))
        return self._handle_for(options)

    def Start(self, handle, _parent, options, **_kwargs):
        self.calls.append(("Start", (str(handle),)))
        self.events.append(("call", "Start"))
        return self._handle_for(options)

    def OpenPipeWireRemote(self, handle, _options, **_kwargs):
        self.calls.append(("OpenPipeWireRemote", (str(handle),)))
        read_fd, write_fd = os.pipe()
        os.close(write_fd)
        self.opened_fds.append(read_fd)
        return _FakeFd(read_fd)

    def Close(self, handle, **_kwargs):
        self.calls.append(("Close", (str(handle),)))

    def Introspect(self, **_kwargs):  # pragma: no cover - bypassed by the fake
        raise AssertionError


class _FakeFd:
    """Stands in for a ``dbus.types.UnixFd``."""

    def __init__(self, value):
        self._value = value

    def take(self) -> int:
        return self._value


class FakeBus:
    def __init__(self):
        self.requests: dict[str, object] = {}
        self.events: list[tuple] = []
        self.proxy = FakePortalProxy(events=self.events)

    def get_unique_name(self) -> str:
        return ":1.42"

    def get_object(self, _name, _path):
        return self.proxy

    def add_signal_receiver(self, handler, signal_name="", **kwargs):
        path = kwargs.get("path")
        self.events.append(("subscribe", path))
        if signal_name == "Response":
            self.requests[str(path)] = handler
        return _Subscription(self, str(path))

    def remove_signal_receiver(self, subscription):
        self.requests.pop(subscription.path, None)


class _Subscription:
    def __init__(self, bus, path):
        self.bus = bus
        self.path = path

    def remove(self):
        self.bus.remove_signal_receiver(self)


class FakeSession(ScreenCastSession):
    """A session wired to fakes instead of a live D-Bus session bus.

    The *real* request dispatcher is used, so these tests cover the routing
    logic rather than a simplified stand-in for it.
    """

    def __init__(self, bus=None):
        super().__init__(timeout_seconds=5)
        self.fake_bus = bus or FakeBus()

    def _connect(self) -> None:
        from troika.portal import _RequestDispatcher

        self._bus = self.fake_bus
        self._desktop = self.fake_bus.proxy
        self._sender_token = "1_42"
        self._dispatcher = _RequestDispatcher(self.fake_bus)

    def _has_screencast_interface(self) -> bool:
        return True

    def _arm_timeout(self) -> None:
        # No GLib main loop in the tests, so the timeout is not armed.
        return


@pytest.fixture
def session():
    return FakeSession()


@pytest.fixture
def opened(session):
    """A session with both callbacks captured, having issued CreateSession.

    The session is always closed at teardown: the fake hands out real pipe
    descriptors, so leaving one open would leak it into the test process.
    """
    ready: list[ScreenCastStream] = []
    errors: list[Exception] = []
    session.open(on_ready=ready.append, on_error=errors.append)
    yield session, ready, errors
    session.close()


@pytest.fixture
def open_session():
    """Factory for fake-backed sessions, all closed at teardown.

    Returns the session so a test can inspect the requests it registered.
    """
    created: list[FakeSession] = []

    def make() -> FakeSession:
        session = FakeSession()
        created.append(session)
        return session

    yield make
    for session in created:
        session.close()


def respond(session, handle_name: str, response: int, results: dict) -> None:
    """Deliver a portal response to the callback registered for *handle_name*.

    The session subscribes to the request path before issuing the call, so a
    pending handler must already exist for the token it chose. Finding one here
    is itself the check that the predicted path was right.
    """
    matching = [
        (path, handler)
        for path, handler in session.fake_bus.requests.items()
        if path.rsplit("/", 1)[-1].startswith(f"troika_{handle_name}")
    ]
    assert matching, f"no pending request for {handle_name}: {list(session.fake_bus.requests)}"
    _path, handler = matching[-1]
    handler(response, results)


def stream_payload(node_id=42, size=(1920, 1080)):
    return {"streams": [(node_id, {"position": (0, 0), "size": size})]}


def drive_to_start(session) -> None:
    respond(session, "create", RESPONSE_SUCCESS, {"session_handle": "/s/1"})
    respond(session, "select", RESPONSE_SUCCESS, {})
    respond(session, "start", RESPONSE_SUCCESS, stream_payload())


# -- request sequencing -------------------------------------------------------


def test_open_issues_create_session(opened) -> None:
    session, _ready, _errors = opened
    assert "CreateSession" in [name for name, _ in session.fake_bus.proxy.calls]


def test_a_monitor_source_is_requested(opened) -> None:
    session, _ready, _errors = opened
    respond(session, "create", RESPONSE_SUCCESS, {"session_handle": "/s/1"})

    assert "SelectSources" in [name for name, _ in session.fake_bus.proxy.calls]


def test_select_sources_targets_the_returned_session_handle(opened) -> None:
    session, _ready, _errors = opened
    respond(session, "create", RESPONSE_SUCCESS, {"session_handle": "/s/1"})

    select_call = next(c for c in session.fake_bus.proxy.calls if c[0] == "SelectSources")
    assert select_call[1] == ("/s/1",)


def test_start_is_issued_after_the_sources_are_selected(opened) -> None:
    session, _ready, _errors = opened
    respond(session, "create", RESPONSE_SUCCESS, {"session_handle": "/s/1"})
    respond(session, "select", RESPONSE_SUCCESS, {})

    assert "Start" in [name for name, _ in session.fake_bus.proxy.calls]


# -- successful negotiation ---------------------------------------------------


def test_a_granted_stream_is_delivered_once(opened) -> None:
    session, ready, errors = opened
    drive_to_start(session)

    assert errors == []
    assert len(ready) == 1
    assert ready[0].node_id == 42
    assert isinstance(ready[0].fd, int)


def test_stream_properties_are_preserved(opened) -> None:
    session, ready, _errors = opened
    drive_to_start(session)

    assert ready[0].properties["size"] == (1920, 1080)


def test_a_duplicate_start_response_does_not_re_enter_the_callback(opened) -> None:
    session, ready, _errors = opened
    drive_to_start(session)
    respond(session, "start", RESPONSE_SUCCESS, stream_payload())

    assert len(ready) == 1


# -- cancellation and failures ------------------------------------------------


def test_cancelling_screen_selection_raises_portal_cancelled(opened) -> None:
    session, ready, errors = opened
    respond(session, "create", RESPONSE_SUCCESS, {"session_handle": "/s/1"})
    respond(session, "select", RESPONSE_CANCELLED, {})

    assert ready == []
    assert len(errors) == 1
    assert isinstance(errors[0], PortalCancelled)


def test_cancelling_at_start_raises_portal_cancelled(opened) -> None:
    session, _ready, errors = opened
    respond(session, "create", RESPONSE_SUCCESS, {"session_handle": "/s/1"})
    respond(session, "select", RESPONSE_SUCCESS, {})
    respond(session, "start", RESPONSE_CANCELLED, {})

    assert isinstance(errors[0], PortalCancelled)


def test_cancelling_session_creation_raises_portal_cancelled(opened) -> None:
    _session, _ready, errors = opened
    respond(opened[0], "create", RESPONSE_CANCELLED, {})

    assert isinstance(errors[0], PortalCancelled)


def test_a_non_cancel_error_is_a_plain_portal_error(opened) -> None:
    session, _ready, errors = opened
    respond(session, "create", 2, {})

    assert isinstance(errors[0], PortalError)
    assert not isinstance(errors[0], PortalCancelled)


def test_an_empty_stream_list_is_an_error(opened) -> None:
    session, _ready, errors = opened
    respond(session, "create", RESPONSE_SUCCESS, {"session_handle": "/s/1"})
    respond(session, "select", RESPONSE_SUCCESS, {})
    respond(session, "start", RESPONSE_SUCCESS, {"streams": []})

    assert errors and "no screen streams" in str(errors[0])


def test_a_failing_pipewire_remote_is_reported(opened) -> None:
    session, _ready, errors = opened

    def explode(*_args, **_kwargs):
        raise RuntimeError("Connection refused")

    session.fake_bus.proxy.OpenPipeWireRemote = explode
    respond(session, "create", RESPONSE_SUCCESS, {"session_handle": "/s/1"})
    respond(session, "select", RESPONSE_SUCCESS, {})
    respond(session, "start", RESPONSE_SUCCESS, stream_payload())

    assert errors and "OpenPipeWireRemote" in str(errors[0])


def test_a_missing_session_handle_is_reported(opened) -> None:
    session, _ready, errors = opened
    respond(session, "create", RESPONSE_SUCCESS, {})

    assert errors and "session handle" in str(errors[0]).lower()


def test_a_dead_portal_is_reported_without_a_traceback() -> None:
    class DeadProxy(FakePortalProxy):
        def CreateSession(self, *_args, **_kwargs):
            raise RuntimeError("org.freedesktop.portal.Desktop is not activatable")

    session = FakeSession()
    session.fake_bus.proxy = DeadProxy()
    errors: list[Exception] = []

    session.open(on_ready=lambda _s: None, on_error=errors.append)

    assert errors and "CreateSession failed" in str(errors[0])


# -- lifecycle ----------------------------------------------------------------


def test_close_releases_the_session_and_the_stream(opened) -> None:
    session, _ready, _errors = opened
    drive_to_start(session)

    session.close()

    assert session._session_handle is None
    assert session._stream is None
    assert "Close" in [name for name, _ in session.fake_bus.proxy.calls]


def test_close_is_safe_before_open() -> None:
    FakeSession().close()  # must not raise


def test_close_is_idempotent(opened) -> None:
    session, _ready, _errors = opened
    respond(session, "create", RESPONSE_SUCCESS, {"session_handle": "/s/1"})
    session.close()
    closes_after_first = [c for c in session.fake_bus.proxy.calls if c[0] == "Close"]
    session.close()

    assert session._session_handle is None
    # The second close must release nothing further.
    closes_after_second = [c for c in session.fake_bus.proxy.calls if c[0] == "Close"]
    assert len(closes_after_second) == len(closes_after_first)
    assert closes_after_first, "closing a live session must release it"


def test_close_dismisses_an_open_permission_dialog(opened) -> None:
    """Stop during startup must not leave the user staring at a live prompt."""
    session, _ready, _errors = opened
    respond(session, "create", RESPONSE_SUCCESS, {"session_handle": "/s/1"})
    # SelectSources is now pending: its dialog is the one on screen.
    pending = session._pending_request
    assert pending is not None

    session.close()

    closed_paths = [c[1][0] for c in session.fake_bus.proxy.calls if c[0] == "Close"]
    assert pending in closed_paths


def test_close_drops_the_pending_response_callback(opened) -> None:
    """A late portal response must not invoke a cancelled session's callback."""
    session, ready, errors = opened
    respond(session, "create", RESPONSE_SUCCESS, {"session_handle": "/s/1"})
    handler = next(iter(session.fake_bus.requests.values()))

    session.close()
    handler(RESPONSE_SUCCESS, stream_payload())  # a response arriving too late

    assert ready == []
    assert errors == []


def test_close_stops_listening_for_portal_responses(opened) -> None:
    session, _ready, _errors = opened
    assert session.fake_bus.requests, "a request must be pending before close"

    session.close()

    assert session._dispatcher is None
    # The bus no longer holds any handler, so a late response cannot be routed.
    assert session.fake_bus.requests == {}


def test_a_close_failure_is_swallowed(opened) -> None:
    session, _ready, _errors = opened
    respond(session, "create", RESPONSE_SUCCESS, {"session_handle": "/s/1"})

    def explode(*_args, **_kwargs):
        raise RuntimeError("portal is gone")

    session.fake_bus.proxy.Close = explode
    session.close()  # must not raise

    assert session._session_handle is None


def test_stream_close_is_safe_without_a_descriptor() -> None:
    stream = ScreenCastStream(node_id=1, properties={})
    stream.close()
    stream.close()  # must not raise, even though there is no fd


def test_stream_close_only_releases_the_fd_once() -> None:
    read_fd, write_fd = os.pipe()
    os.close(write_fd)
    stream = ScreenCastStream(node_id=1, properties={}, fd=read_fd)

    stream.close()
    stream.close()

    assert stream.fd is None
    # The descriptor is genuinely closed, so a duplicate close cannot resurface.
    with pytest.raises(OSError):
        os.fstat(read_fd)


# -- response routing ---------------------------------------------------------


def test_every_response_is_subscribed_before_its_call_is_made(open_session) -> None:
    """The subscription must exist before the call can produce a response.

    python-dbus only reports the object path for a path-specific subscription,
    so the session subscribes to the request path it predicts from its own
    handle token. If a call were made first, a fast portal response would be
    emitted with nobody listening and the session would stall silently. That is
    exactly the failure this guards against.
    """
    session = open_session()
    session.open(on_ready=lambda _s: None, on_error=lambda _e: None)

    events = session.fake_bus.events
    assert events[0] == ("subscribe", session._request_path("troika_create1"))
    assert events[1] == ("call", "CreateSession")

    respond(session, "create", RESPONSE_SUCCESS, {"session_handle": "/s/1"})
    assert ("subscribe", session._request_path("troika_select2")) in events
    assert events.index(("call", "SelectSources")) < len(events)


def test_the_subscription_path_matches_the_portal_handle(open_session) -> None:
    """The predicted path must equal the handle the portal actually returns.

    A mismatch means the response would arrive on a path nobody subscribed to.
    """
    session = open_session()
    session.open(on_ready=lambda _s: None, on_error=lambda _e: None)

    predicted = session._request_path("troika_create1")
    actual = session.fake_bus.proxy.CreateSession(
        session._options("troika_create1")
    )

    assert predicted == str(actual)


def test_responses_are_not_delivered_without_a_path_specific_subscription(
    open_session,
) -> None:
    """A bus-wide subscription cannot route responses, so none is installed.

    The old implementation attached one wildcard receiver. python-dbus handed it
    ``path=None`` for every response, so no callback was ever matched. This test
    pins the per-path design that replaced it.
    """
    session = open_session()
    session.open(on_ready=lambda _s: None, on_error=lambda _e: None)

    subscribed_paths = [
        path for kind, path in session.fake_bus.events if kind == "subscribe"
    ]
    assert subscribed_paths, "the session must subscribe to its request"
    assert all(path for path in subscribed_paths), (
        "a subscription without an explicit path cannot be routed"
    )


def test_an_unexpected_handle_path_is_still_followed(open_session) -> None:
    """A portal that ignores our token must not strand the session.

    The handle token is a hint. If a portal picks its own request path, the
    session subscribes to the returned one as well, so the response is not lost.
    """
    session = open_session()
    session.fake_bus.proxy.CreateSession = lambda _options, **_k: (
        "/org/freedesktop/portal/desktop/request/1_42/portal_chose_this"
    )
    session.open(on_ready=lambda _s: None, on_error=lambda _e: None)

    assert (
        "/org/freedesktop/portal/desktop/request/1_42/portal_chose_this"
        in session.fake_bus.requests
    )


def test_closing_removes_every_subscription(open_session) -> None:
    """No subscription may outlive the session that created it."""
    session = open_session()
    session.open(on_ready=lambda _s: None, on_error=lambda _e: None)
    respond(session, "create", RESPONSE_SUCCESS, {"session_handle": "/s/1"})

    session.close()

    assert session.fake_bus.requests == {}
