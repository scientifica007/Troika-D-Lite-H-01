"""XDG Desktop Portal ScreenCast client.

On Wayland a client cannot read the framebuffer directly. The supported path is
the ScreenCast portal, which asks the compositor for a stream and hands back a
PipeWire file descriptor plus a node id. ``pipewiresrc`` then consumes that
node, and the compositor does the compositing and scaling.

The portal API is asynchronous: each call returns a *request handle*, and the
actual result arrives later as a ``Response`` signal on that handle. A single
D-Bus signal subscription is installed for all responses and dispatches by
object path, which removes the race where a response is emitted before the
caller knows which path to watch.

Nothing here requires root. If the user declines the permission dialog, or the
portal is missing, a :class:`PortalError` is raised and the caller returns to a
clean idle state.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable

BUS_NAME = "org.freedesktop.portal.Desktop"
OBJECT_PATH = "/org/freedesktop/portal/desktop"
SCREENCAST_IFACE = "org.freedesktop.portal.ScreenCast"
REQUEST_IFACE = "org.freedesktop.portal.Request"
SESSION_IFACE = "org.freedesktop.portal.Session"

#: ``types`` bit for a full monitor rather than a single window.
SOURCE_TYPE_MONITOR = 1

#: ``cursor_mode`` value that composites the pointer into the stream.
CURSOR_MODE_EMBEDDED = 1

#: Response codes returned by the portal.
RESPONSE_SUCCESS = 0
RESPONSE_CANCELLED = 1
RESPONSE_OTHER_ERROR = 2

#: How long to wait for the compositor to hand back a stream before giving up.
DEFAULT_TIMEOUT_SECONDS = 120


class PortalError(RuntimeError):
    """Raised when a screen capture session cannot be established."""


class PortalCancelled(PortalError):
    """Raised when the user dismisses or denies the permission dialog."""


@dataclass
class ScreenCastStream:
    """The PipeWire stream the compositor granted us."""

    node_id: int
    properties: dict = field(default_factory=dict)
    fd: int | None = None

    def close(self) -> None:
        """Release the PipeWire file descriptor."""
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None


class _RequestDispatcher:
    """Routes portal ``Response`` signals to per-request callbacks.

    Every request gets its own exact-path subscription. A single bus-wide
    receiver does not work here: python-dbus only reports the object path a
    signal arrived on when the subscription is path-specific, so a wildcard
    receiver sees the response but cannot tell which request produced it. The
    request path is deterministic (``.../request/<sender>/<token>``), so the
    path is known before the call is made and nothing can be missed.
    """

    def __init__(self, bus) -> None:
        self._bus = bus
        self._matches: dict[str, object] = {}

    def expect(self, request_path: str, callback: Callable[[int, dict], None]) -> None:
        path = str(request_path)
        if path in self._matches:
            return
        self._matches[path] = self._bus.add_signal_receiver(
            lambda response, results: callback(int(response), dict(results or {})),
            signal_name="Response",
            dbus_interface=REQUEST_IFACE,
            path=path,
        )

    def close(self) -> None:
        """Drop every pending subscription.

        Leaving them attached would let a late response from a finished session
        invoke a stale callback.
        """
        matches, self._matches = self._matches, {}
        for match in matches.values():
            self._remove(match)

    def _remove(self, match) -> None:
        if match is None:
            return
        remove = getattr(match, "remove", None)
        try:
            if remove is not None:
                remove()
            else:  # pragma: no cover - depends on the python-dbus version
                self._bus.remove_signal_receiver(match)
        except Exception:
            pass


class ScreenCastSession:
    """A live ScreenCast portal session.

    Usage::

        session = ScreenCastSession()
        stream = session.open(callback)     # asynchronous
        ...
        session.close()

    The session object owns the D-Bus session handle and the PipeWire fd, so
    closing it releases everything the portal granted.
    """

    def __init__(self, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        self._timeout_seconds = timeout_seconds
        self._bus = None
        self._desktop = None
        self._dispatcher: _RequestDispatcher | None = None
        self._session_handle: str | None = None
        self._stream: ScreenCastStream | None = None
        self._closed = False
        self._pending_timeout: int | None = None
        self._pending_request: str | None = None
        self._counter = 0
        self._sender_token = ""
        self._callback: Callable[[ScreenCastStream], None] | None = None
        self._error_callback: Callable[[Exception], None] | None = None

    # -- public API ---------------------------------------------------------

    @property
    def stream(self) -> ScreenCastStream | None:
        return self._stream

    def open(
        self,
        on_ready: Callable[[ScreenCastStream], None],
        on_error: Callable[[Exception], None],
    ) -> None:
        """Begin a capture session. Returns immediately.

        Exactly one of *on_ready* or *on_error* is called, from the GLib main
        loop. The permission dialog appears as a side effect of ``Start``.
        """
        self._callback = on_ready
        self._error_callback = on_error
        try:
            self._connect()
        except Exception as exc:
            self._fail(exc)
            return
        self._arm_timeout()
        self._create_session()

    def close(self) -> None:
        """Release the portal session and the PipeWire fd. Safe to call twice.

        Calling this while the permission dialog is still open aborts the
        pending request: the portal is asked to close the request handle, which
        dismisses the dialog, and the callback registered for it is dropped.
        """
        if self._closed:
            return
        self._closed = True
        self._cancel_timeout()
        self._close_pending_request()
        if self._stream is not None:
            self._stream.close()
            self._stream = None
        if self._session_handle and self._desktop is not None:
            try:
                self._desktop.Close(
                    self._session_handle, dbus_interface=SESSION_IFACE
                )
            except Exception:
                # The portal may already have torn the session down (for
                # example after a compositor restart). Nothing left to release.
                pass
        self._session_handle = None
        self._desktop = None
        if self._dispatcher is not None:
            self._dispatcher.close()
        self._dispatcher = None
        self._callback = None
        self._error_callback = None
        self._bus = None

    def _close_pending_request(self) -> None:
        """Ask the portal to drop the request whose dialog is still open."""
        request = self._pending_request
        self._pending_request = None
        if request is None or self._desktop is None:
            return
        import dbus

        try:
            self._desktop.Close(
                dbus.ObjectPath(request), dbus_interface=REQUEST_IFACE
            )
        except Exception:
            # Best effort: the request may have just been answered, in which
            # case there is nothing left to close.
            pass

    # -- internals ----------------------------------------------------------

    def _connect(self) -> None:
        try:
            import dbus
            from dbus.mainloop.glib import DBusGMainLoop
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise PortalError(
                "Python D-Bus bindings are missing. Install python3-dbus."
            ) from exc

        DBusGMainLoop(set_as_default=True)
        try:
            self._bus = dbus.SessionBus()
        except Exception as exc:
            raise PortalError(f"No D-Bus session bus available: {exc}") from exc

        self._sender_token = self._bus.get_unique_name()[1:].replace(".", "_")
        self._dispatcher = _RequestDispatcher(self._bus)

        try:
            self._desktop = self._bus.get_object(BUS_NAME, OBJECT_PATH)
        except Exception as exc:
            raise PortalError(f"Cannot reach the desktop portal: {exc}") from exc

        if not self._has_screencast_interface():
            raise PortalError(
                "The desktop portal does not provide ScreenCast. "
                "Install xdg-desktop-portal and a Wayland backend such as "
                "xdg-desktop-portal-gnome, -kde, -wlr or -hyprland."
            )

    def _has_screencast_interface(self) -> bool:
        try:
            introspect = self._desktop.Introspect(
                dbus_interface="org.freedesktop.DBus.Introspectable"
            )
        except Exception:
            return False
        return SCREENCAST_IFACE in str(introspect)

    def _next_token(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}{self._counter}"

    def _options(self, token: str, **extra) -> "object":
        import dbus

        values = {"handle_token": dbus.String(token)}
        values.update(extra)
        return dbus.Dictionary(values, signature="sv")

    def _expect(self, request_path, callback) -> None:
        assert self._dispatcher is not None
        # Remembered so close() can ask the portal to dismiss the dialog this
        # request owns, instead of leaving the user staring at a dead prompt.
        self._pending_request = str(request_path)
        self._dispatcher.expect(str(request_path), callback)

    def _request_path(self, token: str) -> str:
        """The request object path the portal will use for *token*.

        The portal builds it from our unique name and the handle token, so it is
        known before the call is made. That matters: the subscription has to be
        in place first, or a fast response would be missed.
        """
        return (
            f"{OBJECT_PATH}/request/{self._sender_token}/{token}"
        )

    def _arm(self, token: str, callback) -> str:
        """Subscribe for *token*'s response and return its request path."""
        path = self._request_path(token)
        self._expect(path, callback)
        return path

    def _rearm_if_needed(self, handle, expected_path: str, callback) -> None:
        """Follow the portal's actual request path if it ignored our token.

        The handle token is only a hint; a portal is free to pick its own path.
        The call has already returned by now, so re-subscribing cannot miss a
        response that has not been emitted yet.
        """
        actual = str(handle)
        if actual and actual != expected_path:
            self._expect(actual, callback)

    def _create_session(self) -> None:
        import dbus

        token = self._next_token("troika_create")
        expected = self._arm(token, self._on_session_created)
        try:
            handle = self._desktop.CreateSession(
                self._options(
                    token, session_handle_token=dbus.String(f"troika_{self._sender_token}")
                ),
                dbus_interface=SCREENCAST_IFACE,
            )
        except Exception as exc:
            self._fail(PortalError(f"CreateSession failed: {exc}"))
            return
        self._rearm_if_needed(handle, expected, self._on_session_created)

    def _on_session_created(self, response: int, results: dict) -> None:
        if response != RESPONSE_SUCCESS:
            self._fail(self._response_error("create a capture session", response))
            return
        self._session_handle = str(results.get("session_handle", ""))
        if not self._session_handle:
            self._fail(PortalError("The portal returned no session handle."))
            return
        self._select_sources()

    def _select_sources(self) -> None:
        import dbus

        token = self._next_token("troika_select")
        expected = self._arm(token, self._on_sources_selected)
        try:
            handle = self._desktop.SelectSources(
                dbus.ObjectPath(self._session_handle),
                self._options(
                    token,
                    types=dbus.UInt32(SOURCE_TYPE_MONITOR),
                    multiple=dbus.Boolean(False),
                    cursor_mode=dbus.UInt32(CURSOR_MODE_EMBEDDED),
                ),
                dbus_interface=SCREENCAST_IFACE,
            )
        except Exception as exc:
            self._fail(PortalError(f"SelectSources failed: {exc}"))
            return
        self._rearm_if_needed(handle, expected, self._on_sources_selected)

    def _on_sources_selected(self, response: int, _results: dict) -> None:
        if response != RESPONSE_SUCCESS:
            self._fail(self._response_error("select a screen source", response))
            return
        self._start()

    def _start(self) -> None:
        import dbus

        token = self._next_token("troika_start")
        expected = self._arm(token, self._on_started)
        try:
            handle = self._desktop.Start(
                dbus.ObjectPath(self._session_handle),
                "",
                self._options(token),
                dbus_interface=SCREENCAST_IFACE,
            )
        except Exception as exc:
            self._fail(PortalError(f"Start failed: {exc}"))
            return
        self._rearm_if_needed(handle, expected, self._on_started)

    def _on_started(self, response: int, results: dict) -> None:
        if response != RESPONSE_SUCCESS:
            self._fail(self._response_error("start the screen capture", response))
            return

        streams = results.get("streams") or []
        if not streams:
            self._fail(PortalError("The portal granted no screen streams."))
            return

        node_id = int(streams[0][0])
        properties = dict(streams[0][1]) if len(streams[0]) > 1 else {}
        self._stream = ScreenCastStream(node_id=node_id, properties=properties)

        try:
            self._stream.fd = self._open_pipewire_remote()
        except Exception as exc:
            self._fail(PortalError(f"OpenPipeWireRemote failed: {exc}"))
            return

        self._cancel_timeout()
        callback = self._callback
        self._callback = None
        if callback is not None:
            callback(self._stream)

    def _open_pipewire_remote(self) -> int:
        import dbus

        fd_object = self._desktop.OpenPipeWireRemote(
            dbus.ObjectPath(self._session_handle),
            dbus.Dictionary({}, signature="sv"),
            dbus_interface=SCREENCAST_IFACE,
        )
        # ``take()`` hands us the raw descriptor; ``ScreenCastStream.close``
        # owns it from here.
        return int(fd_object.take())

    # -- failure handling ---------------------------------------------------

    def _response_error(self, action: str, response: int) -> PortalError:
        if response == RESPONSE_CANCELLED:
            return PortalCancelled(
                f"The screen sharing request was cancelled while trying to {action}."
            )
        return PortalError(
            f"The desktop portal refused to {action} (response code {response})."
        )

    def _fail(self, error: Exception) -> None:
        self._cancel_timeout()
        callback = self._error_callback
        self._callback = None
        self._error_callback = None
        # Release anything the portal already granted before reporting failure.
        self.close()
        if callback is not None:
            callback(error)

    def _arm_timeout(self) -> None:
        try:
            from gi.repository import GLib
        except ImportError:  # pragma: no cover
            return

        def on_timeout() -> bool:
            self._pending_timeout = None
            self._fail(
                PortalError(
                    "Timed out waiting for the screen sharing permission dialog. "
                    "Make sure the desktop portal and a Wayland backend are running."
                )
            )
            return False

        self._pending_timeout = GLib.timeout_add_seconds(
            int(self._timeout_seconds), on_timeout
        )

    def _cancel_timeout(self) -> None:
        if self._pending_timeout is None:
            return
        try:
            from gi.repository import GLib

            GLib.source_remove(self._pending_timeout)
        except Exception:
            pass
        self._pending_timeout = None
