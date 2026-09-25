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


def _properties_to_dict(props) -> dict[str, object]:
    if props is None:
        return {}
    return {key: props.get_value(key) for key in props.keys()}


def _is_monitor(display_name: str, properties: dict[str, object]) -> bool:
    """Decide whether a source is a playback monitor rather than a microphone."""
    if properties.get("device.class") == "monitor":
        return True
    return display_name.startswith("Monitor of ")


def enumerate_via_gstreamer() -> list[AudioDevice]:
    """Enumerate audio inputs using ``Gst.DeviceMonitor``.

    Returns an empty list if GStreamer is unavailable or finds nothing, so the
    caller can fall back to another mechanism.
    """
    try:
        import gi

        gi.require_version("Gst", "1.0")
        from gi.repository import Gst
    except (ImportError, ValueError):
        return []

    if not Gst.is_initialized():
        Gst.init(None)

    monitor = Gst.DeviceMonitor.new()
    monitor.add_filter("Audio/Source", None)
    if not monitor.start():
        return []

    devices: list[AudioDevice] = []
    try:
        for device in monitor.get_devices():
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
                continue
            display = device.get_display_name() or str(name)
            devices.append(
                AudioDevice(
                    name=str(name),
                    label=display,
                    is_monitor=_is_monitor(display, props),
                    is_default=bool(props.get("is-default")),
                )
            )
    finally:
        monitor.stop()
    return devices


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


def list_input_devices() -> list[AudioDevice]:
    """Return every selectable audio input, microphones first.

    Monitor sources are placed after microphones because the microphone is the
    device the user actively chooses; monitors are normally selected implicitly
    by the audio mode.
    """
    devices = _merge(enumerate_via_gstreamer(), enumerate_via_pactl())
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
