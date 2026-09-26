"""Audio input device enumeration.

Two kinds of audio input matter to this application:

* **microphones** — real capture devices (internal, USB, headset);
* **monitor sources** — the loopback of a playback device, which is how
  "system audio" is captured on PipeWire/PulseAudio.

Both are reported by the sound server as *sources*, so the distinction is made
explicit here rather than left to the caller. Enumeration is done through
GStreamer's device monitor, which resolves each device to the exact ``pulsesrc``
``device`` name we later need. If that fails, we fall back to parsing ``pactl``
JSON so a device list is still available on unusual setups.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass

# How long to wait for the fallback enumeration helper.
_PACTL_TIMEOUT_SECONDS = 5


@dataclass(frozen=True)
class AudioDevice:
    """One selectable audio input."""

    name: str
    label: str
    is_monitor: bool = False
    is_default: bool = False

    @property
    def is_microphone(self) -> bool:
        return not self.is_monitor


def _structure_field_names(props) -> list[str]:
    """Return the field names of a ``Gst.Structure`` on any PyGObject build.

    ``Gst.Structure`` exposes ``n_fields()`` and ``nth_field_name(index)``
    everywhere, but ``keys()`` only on some bindings: on Ubuntu 24.04's
    PyGObject a ``Structure`` has no ``keys`` at all, so relying on it crashed
    device enumeration at startup. The index-based API is the portable one and
    is preferred; ``keys()`` is kept as a fallback for bindings that offer it.
    """
    if props is None:
        return []

    keys = getattr(props, "keys", None)
    if callable(keys):
        try:
            return [str(key) for key in keys()]
        except Exception:
            pass

    names: list[str] = []
    try:
        count = int(props.n_fields())
    except Exception:
        return names
    for index in range(count):
        try:
            names.append(str(props.nth_field_name(index)))
        except Exception:
            continue
    return names


def _properties_to_dict(props) -> dict[str, object]:
    """Convert a ``Gst.Structure`` into a plain dict, tolerating odd values."""
    result: dict[str, object] = {}
    for key in _structure_field_names(props):
        try:
            result[key] = props.get_value(key)
        except Exception:
            # A field we cannot read is not worth failing enumeration over.
            continue
    return result


def _is_monitor(display_name: str, properties: dict[str, object]) -> bool:
    """Decide whether a source is a playback monitor rather than a microphone."""
    if properties.get("device.class") == "monitor":
        return True
    return display_name.startswith("Monitor of ")


def _device_from_gstreamer(device) -> AudioDevice | None:
    """Interpret one ``Gst.Device``, or return ``None`` if it is unusable.

    Kept separate and defensive because the properties of a single device are
    not worth failing the whole enumeration over: the caller still has the
    ``pactl`` fallback, and a device we cannot describe is simply not offered.
    """
    props = _properties_to_dict(device.get_properties())
    # ``node.name`` is what PipeWire and pulsesrc both accept.
    name = props.get("node.name") or props.get("device.name")
    if not name:
        try:
            element = device.create_element(None)
            name = element.get_property("device")
        except Exception:
            name = None
    if not name:
        return None
    display = device.get_display_name() or str(name)
    return AudioDevice(
        name=str(name),
        label=display,
        is_monitor=_is_monitor(display, props),
        is_default=bool(props.get("is-default")),
    )


def enumerate_via_gstreamer() -> list[AudioDevice]:
    """Enumerate audio inputs using ``Gst.DeviceMonitor``.

    Best-effort by contract: this returns whatever it could interpret, and an
    empty list if GStreamer is unavailable, finds nothing, or misbehaves. It
    never raises, so a binding difference or a single unreadable device cannot
    stop the application from starting — the caller falls back to ``pactl``.
    """
    try:
        import gi

        gi.require_version("Gst", "1.0")
        from gi.repository import Gst
    except (ImportError, ValueError):
        return []

    try:
        if not Gst.is_initialized():
            Gst.init(None)

        monitor = Gst.DeviceMonitor.new()
        monitor.add_filter("Audio/Source", None)
        if not monitor.start():
            return []

        devices: list[AudioDevice] = []
        try:
            for device in monitor.get_devices():
                try:
                    parsed = _device_from_gstreamer(device)
                except Exception:
                    parsed = None
                if parsed is not None:
                    devices.append(parsed)
        finally:
            monitor.stop()
        return devices
    except Exception:
        return []


def parse_pactl_sources(payload: str) -> list[AudioDevice]:
    """Parse ``pactl -f json list sources`` output into devices.

    Kept as a pure function so the parsing rules are unit-testable without a
    sound server running.
    """
    try:
        entries = json.loads(payload)
    except (TypeError, ValueError):
        return []
    if not isinstance(entries, list):
        return []

    devices: list[AudioDevice] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not name:
            continue
        properties = entry.get("properties") or {}
        label = properties.get("device.description") or entry.get("description") or name
        devices.append(
            AudioDevice(
                name=str(name),
                label=str(label),
                # ``monitor_source`` is present exactly on monitor sources.
                is_monitor=bool(entry.get("monitor_source"))
                or properties.get("device.class") == "monitor",
                is_default=False,
            )
        )
    return devices


def enumerate_via_pactl() -> list[AudioDevice]:
    """Enumerate audio inputs by asking the sound server directly."""
    try:
        result = subprocess.run(
            ["pactl", "-f", "json", "list", "sources"],
            capture_output=True,
            text=True,
            timeout=_PACTL_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    return parse_pactl_sources(result.stdout)


def _merge(primary: list[AudioDevice], secondary: list[AudioDevice]) -> list[AudioDevice]:
    """Combine two enumerations, preferring the first entry for a given name."""
    merged: dict[str, AudioDevice] = {}
    for device in primary + secondary:
        existing = merged.get(device.name)
        if existing is None:
            merged[device.name] = device
        elif device.is_default and not existing.is_default:
            merged[device.name] = device
    return list(merged.values())


def _safe(enumerator) -> list[AudioDevice]:
    """Run one enumerator, treating any failure as "found nothing".

    Enumeration is best-effort by design, and the application must still start
    when a backend misbehaves — a binding difference or a broken device monitor
    should cost the user a device list, not the whole program. The other
    enumerator is then still consulted.
    """
    try:
        return enumerator()
    except Exception:
        return []


def list_input_devices() -> list[AudioDevice]:
    """Return every selectable audio input, microphones first.

    Monitor sources are placed after microphones because the microphone is the
    device the user actively chooses; monitors are normally selected implicitly
    by the audio mode.
    """
    devices = _merge(
        _safe(enumerate_via_gstreamer),
        _safe(enumerate_via_pactl),
    )
    devices.sort(key=lambda d: (d.is_monitor, not d.is_default, d.label.lower()))
    return devices


def list_microphones() -> list[AudioDevice]:
    """Return only real capture devices (no playback monitors)."""
    return [device for device in list_input_devices() if device.is_microphone]


def list_monitors() -> list[AudioDevice]:
    """Return only playback monitor sources (used for system audio)."""
    return [device for device in list_input_devices() if device.is_monitor]


def find_device(name: str | None) -> AudioDevice | None:
    """Look up a device by name, or return ``None`` if it has disappeared."""
    if not name:
        return None
    for device in list_input_devices():
        if device.name == name:
            return device
    return None


def default_microphone(devices: list[AudioDevice] | None = None) -> AudioDevice | None:
    """Choose the microphone to preselect in the UI.

    Preference order: the server's default, then the first available device.
    """
    candidates = [d for d in (devices if devices is not None else list_microphones()) if d.is_microphone]
    if not candidates:
        return None
    for device in candidates:
        if device.is_default:
            return device
    return candidates[0]


def default_monitor(devices: list[AudioDevice] | None = None) -> AudioDevice | None:
    """Choose the monitor source used for system audio.

    The first available monitor is used. Any monitor captures the same mix on a
    normal desktop, and preferring the default sink's monitor avoids having to
    track which output is currently active.
    """
    candidates = devices if devices is not None else list_monitors()
    monitors = [d for d in candidates if d.is_monitor]
    if not monitors:
        return None
    for device in monitors:
        if device.is_default:
            return device
    return monitors[0]
