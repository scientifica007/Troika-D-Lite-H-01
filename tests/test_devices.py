"""Tests for audio device enumeration and selection logic.

These run without a sound server: the GStreamer enumeration is monkeypatched,
and the ``pactl`` parsing is tested directly against realistic payloads.
"""

from __future__ import annotations

import json

from troika import devices
from troika.devices import AudioDevice

PACTL_PAYLOAD = json.dumps(
    [
        {
            "name": "alsa_input.pci-0000_00_1b.0.analog-stereo",
            "description": "Built-in Audio Analog Stereo",
            "properties": {"device.description": "Built-in Audio Analog Stereo"},
        },
        {
            "name": "alsa_input.usb-Yeti-00.analog-stereo",
            "description": "Blue Yeti",
            "properties": {"device.description": "Blue Yeti"},
        },
        {
            "name": "auto_null.monitor",
            "description": "Monitor of Dummy Output",
            "monitor_source": "auto_null",
            "properties": {
                "device.description": "Monitor of Dummy Output",
                "device.class": "monitor",
            },
        },
    ]
)


def test_parse_pactl_separates_microphones_from_monitors() -> None:
    parsed = devices.parse_pactl_sources(PACTL_PAYLOAD)
    microphones = [d for d in parsed if d.is_microphone]
    monitors = [d for d in parsed if d.is_monitor]

    assert {d.name for d in microphones} == {
        "alsa_input.pci-0000_00_1b.0.analog-stereo",
        "alsa_input.usb-Yeti-00.analog-stereo",
    }
    assert [d.name for d in monitors] == ["auto_null.monitor"]


def test_parse_pactl_uses_the_human_readable_description() -> None:
    parsed = devices.parse_pactl_sources(PACTL_PAYLOAD)
    labels = {d.name: d.label for d in parsed}
    assert labels["alsa_input.usb-Yeti-00.analog-stereo"] == "Blue Yeti"


def test_parse_pactl_tolerates_junk() -> None:
    assert devices.parse_pactl_sources("not json") == []
    assert devices.parse_pactl_sources("{}") == []
    assert devices.parse_pactl_sources('[{"description": "no name"}]') == []


def test_monitor_detection_by_display_name_alone() -> None:
    # Some servers only mark a monitor in the display name.
    assert devices._is_monitor("Monitor of Speakers", {}) is True
    assert devices._is_monitor("Blue Yeti", {}) is False


def test_monitor_detection_by_device_class() -> None:
    assert devices._is_monitor("Something", {"device.class": "monitor"}) is True


def test_merge_prefers_the_default_entry(monkeypatch) -> None:
    plain = AudioDevice(name="mic", label="Mic", is_default=False)
    default = AudioDevice(name="mic", label="Mic (default)", is_default=True)
    merged = devices._merge([plain], [default])
    assert len(merged) == 1
    assert merged[0].is_default is True


def test_merge_keeps_distinct_devices() -> None:
    merged = devices._merge(
        [AudioDevice(name="a", label="A")], [AudioDevice(name="b", label="B")]
    )
    assert {d.name for d in merged} == {"a", "b"}


def test_default_microphone_prefers_the_server_default() -> None:
    chosen = devices.default_microphone(
        [
            AudioDevice(name="a", label="A"),
            AudioDevice(name="b", label="B", is_default=True),
        ]
    )
    assert chosen is not None and chosen.name == "b"


def test_default_microphone_ignores_monitors() -> None:
    chosen = devices.default_microphone(
        [AudioDevice(name="mon", label="Monitor", is_monitor=True)]
    )
    assert chosen is None


def test_default_monitor_selects_a_monitor() -> None:
    chosen = devices.default_monitor(
        [
            AudioDevice(name="mic", label="Mic"),
            AudioDevice(name="mon", label="Monitor", is_monitor=True),
        ]
    )
    assert chosen is not None and chosen.name == "mon"


def test_list_microphones_filters_monitors(monkeypatch) -> None:
    monkeypatch.setattr(
        devices,
        "enumerate_via_gstreamer",
        lambda: [
            AudioDevice(name="mic", label="Mic"),
            AudioDevice(name="mon", label="Monitor", is_monitor=True),
        ],
    )
    monkeypatch.setattr(devices, "enumerate_via_pactl", lambda: [])

    microphones = devices.list_microphones()
    assert [d.name for d in microphones] == ["mic"]


def test_hot_plug_is_visible_after_a_refresh(monkeypatch) -> None:
    # A device connected after startup appears once enumeration is repeated.
    available: list[AudioDevice] = [AudioDevice(name="mic", label="Mic")]
    monkeypatch.setattr(devices, "enumerate_via_gstreamer", lambda: list(available))
    monkeypatch.setattr(devices, "enumerate_via_pactl", lambda: [])

    assert [d.name for d in devices.list_microphones()] == ["mic"]

    available.append(AudioDevice(name="usb", label="USB Mic"))
    assert [d.name for d in devices.list_microphones()] == ["mic", "usb"]


def test_find_device_reports_a_removed_device(monkeypatch) -> None:
    monkeypatch.setattr(
        devices,
        "enumerate_via_gstreamer",
        lambda: [AudioDevice(name="mic", label="Mic")],
    )
    monkeypatch.setattr(devices, "enumerate_via_pactl", lambda: [])

    assert devices.find_device("mic") is not None
    assert devices.find_device("gone") is None
    assert devices.find_device(None) is None


def test_list_input_devices_sorts_microphones_first(monkeypatch) -> None:
    monkeypatch.setattr(
        devices,
        "enumerate_via_gstreamer",
        lambda: [
            AudioDevice(name="mon", label="Monitor", is_monitor=True),
            AudioDevice(name="mic", label="Mic"),
        ],
    )
    monkeypatch.setattr(devices, "enumerate_via_pactl", lambda: [])

    ordered = devices.list_input_devices()
    assert ordered[0].name == "mic"
