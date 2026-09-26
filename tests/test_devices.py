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


class _StructureWithoutKeys:
    """A ``Gst.Structure`` as PyGObject on Ubuntu 24.04 presents it.

    That build exposes no ``keys()`` at all, only the index-based field API.
    Reading properties through ``keys()`` therefore raised
    ``AttributeError: 'Structure' object has no attribute 'keys'`` and killed
    the GUI at startup. This stand-in keeps that shape so the regression cannot
    come back.
    """

    def __init__(self, fields: dict[str, object]) -> None:
        self._fields = list(fields.items())

    def n_fields(self) -> int:
        return len(self._fields)

    def nth_field_name(self, index: int) -> str:
        return self._fields[index][0]

    def get_value(self, name: str):
        for key, value in self._fields:
            if key == name:
                return value
        raise KeyError(name)


def test_properties_are_read_without_a_keys_method() -> None:
    props = _StructureWithoutKeys(
        {"node.name": "alsa_input.usb-Yeti", "device.class": "monitor"}
    )
    assert not hasattr(props, "keys")

    assert devices._properties_to_dict(props) == {
        "node.name": "alsa_input.usb-Yeti",
        "device.class": "monitor",
    }


def test_properties_use_keys_when_the_binding_provides_it() -> None:
    class _WithKeys:
        def __init__(self, fields: dict[str, object]) -> None:
            self._fields = fields

        def keys(self):
            return self._fields.keys()

        def get_value(self, name: str):
            return self._fields[name]

    props = _WithKeys({"node.name": "mic", "is-default": True})
    assert devices._properties_to_dict(props) == {
        "node.name": "mic",
        "is-default": True,
    }


def test_properties_tolerate_a_structure_with_nothing_readable() -> None:
    class _Broken:
        def n_fields(self) -> int:
            raise RuntimeError("binding is unhappy")

    assert devices._properties_to_dict(_Broken()) == {}
    assert devices._properties_to_dict(None) == {}


def test_an_unreadable_field_does_not_discard_the_others() -> None:
    class _PartlyBroken(_StructureWithoutKeys):
        def get_value(self, name: str):
            if name == "device.class":
                raise RuntimeError("unreadable")
            return super().get_value(name)

    props = _PartlyBroken({"node.name": "mic", "device.class": "monitor"})
    assert devices._properties_to_dict(props) == {"node.name": "mic"}


def test_device_is_interpreted_without_a_keys_method() -> None:
    class _Device:
        def get_properties(self):
            return _StructureWithoutKeys(
                {"node.name": "alsa_input.usb-Yeti", "is-default": True}
            )

        def get_display_name(self) -> str:
            return "Blue Yeti"

    parsed = devices._device_from_gstreamer(_Device())
    assert parsed is not None
    assert parsed.name == "alsa_input.usb-Yeti"
    assert parsed.label == "Blue Yeti"
    assert parsed.is_microphone


def test_one_unreadable_device_does_not_stop_enumeration(monkeypatch) -> None:
    class _Good:
        def get_properties(self):
            return _StructureWithoutKeys({"node.name": "mic"})

        def get_display_name(self) -> str:
            return "Mic"

    class _Bad:
        def get_properties(self):
            raise RuntimeError("device is unreadable")

        def get_display_name(self) -> str:
            return "Bad"

    class _Monitor:
        def __init__(self) -> None:
            self._devices = [_Bad(), _Good()]

        def add_filter(self, *_args) -> None:
            pass

        def start(self) -> bool:
            return True

        def get_devices(self):
            return list(self._devices)

        def stop(self) -> None:
            pass

    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst

    monkeypatch.setattr(Gst, "DeviceMonitor", type("M", (), {"new": staticmethod(_Monitor)}))
    monkeypatch.setattr(Gst, "is_initialized", staticmethod(lambda: True))

    found = devices.enumerate_via_gstreamer()
    assert [d.name for d in found] == ["mic"]


def test_gstreamer_enumeration_failure_falls_back_to_pactl(monkeypatch) -> None:
    def _explode():
        raise RuntimeError("the device monitor blew up")

    monkeypatch.setattr(devices, "enumerate_via_gstreamer", _explode)
    monkeypatch.setattr(
        devices,
        "enumerate_via_pactl",
        lambda: [AudioDevice(name="alsa_input.mic", label="Mic")],
    )

    assert [d.name for d in devices.list_input_devices()] == ["alsa_input.mic"]


def test_gstreamer_enumeration_never_raises(monkeypatch) -> None:
    """The documented contract: this is best-effort and cannot crash the UI."""

    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst

    class _ExplodingMonitor:
        @staticmethod
        def new():
            raise RuntimeError("no device monitor today")

    monkeypatch.setattr(Gst, "DeviceMonitor", _ExplodingMonitor)
    monkeypatch.setattr(Gst, "is_initialized", staticmethod(lambda: True))

    assert devices.enumerate_via_gstreamer() == []
