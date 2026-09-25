"""The single application window.

Deliberately small: one window, one column of controls, no live preview. There
is no preview because decoding and rendering the capture stream back to the
user would roughly double the cost of recording on the hardware this program
targets, and a recorder's job is to write a file, not to show one.

The UI owns no recording logic. It builds a :class:`RecordingConfig` from the
widgets, hands it to the :class:`~troika.recorder.Recorder`, and reflects the
state and elapsed time it is told about.
"""

from __future__ import annotations

from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk  # noqa: E402

from . import APP_NAME, __version__  # noqa: E402
from .config import ConfigError, RecordingConfig, default_output_dir  # noqa: E402
from .devices import (  # noqa: E402
    AudioDevice,
    default_microphone,
    find_device,
    list_input_devices,
)
from .modes import AudioMode, FrameRate, RecordingType  # noqa: E402
from .recorder import Recorder, RecorderState  # noqa: E402

_AUDIO_MODE_LABELS = [
    ("No audio", AudioMode.NONE),
    ("System audio", AudioMode.SYSTEM),
    ("Microphone", AudioMode.MICROPHONE),
    ("System audio + microphone", AudioMode.SYSTEM_AND_MICROPHONE),
]

#: Diagnostics are switched on only when this is set in the environment, so the
#: normal recording path stays free of instrumentation.
DIAGNOSTICS_ENV = "TROIKA_DIAGNOSTICS"


class RecorderWindow(Gtk.ApplicationWindow):
    """Main window: choose what to record, then record it."""

    def __init__(self, application: Gtk.Application, diagnostics_enabled: bool) -> None:
        super().__init__(application=application)
        self.set_title(APP_NAME)
        self.set_default_size(520, -1)
        self.set_resizable(True)

        self._devices: list[AudioDevice] = []
        self._diagnostics_enabled = diagnostics_enabled
        self._last_output: Path | None = None
        self._timer_source = None

        self._recorder = Recorder(
            on_state_changed=self._on_state_changed,
            on_error=self._on_error,
            on_finished=self._on_finished,
            diagnostics_enabled=diagnostics_enabled,
        )

        self._build_ui()
        self.refresh_devices()
        self._sync_sensitivity(RecorderState.IDLE)

    # -- construction -------------------------------------------------------

    def _build_ui(self) -> None:
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        root.set_margin_top(16)
        root.set_margin_bottom(16)
        root.set_margin_start(16)
        root.set_margin_end(16)
        self.set_child(root)

        grid = Gtk.Grid(column_spacing=12, row_spacing=10)
        grid.set_hexpand(True)
        root.append(grid)
        row = 0

        # Recording type ---------------------------------------------------
        grid.attach(self._label("Recording type"), 0, row, 1, 1)
        self._type_combo = Gtk.DropDown.new_from_strings(
            ["Audio only", "Screen recording"]
        )
        self._type_combo.set_hexpand(True)
        self._type_combo.set_selected(1)
        self._type_combo.connect("notify::selected", self._on_type_changed)
        grid.attach(self._type_combo, 1, row, 1, 1)
        row += 1

        # Frame rate -------------------------------------------------------
        self._fps_label = self._label("Frame rate")
        grid.attach(self._fps_label, 0, row, 1, 1)
        self._fps_combo = Gtk.DropDown.new_from_strings(["15 FPS", "30 FPS"])
        self._fps_combo.set_selected(1)
        grid.attach(self._fps_combo, 1, row, 1, 1)
        row += 1

        # Audio mode -------------------------------------------------------
        self._audio_label = self._label("Audio")
        grid.attach(self._audio_label, 0, row, 1, 1)
        self._audio_combo = Gtk.DropDown.new_from_strings(
            [label for label, _mode in _AUDIO_MODE_LABELS]
        )
        self._audio_combo.set_selected(0)
        self._audio_combo.connect("notify::selected", self._on_audio_changed)
        grid.attach(self._audio_combo, 1, row, 1, 1)
        row += 1

        # Microphone -------------------------------------------------------
        self._mic_label = self._label("Microphone")
        grid.attach(self._mic_label, 0, row, 1, 1)
        self._mic_combo = Gtk.DropDown.new_from_strings(["No microphone found"])
        self._mic_combo.set_hexpand(True)
        grid.attach(self._mic_combo, 1, row, 1, 1)
        row += 1

        # Refresh ----------------------------------------------------------
        self._refresh_button = Gtk.Button(label="Refresh devices")
        self._refresh_button.connect("clicked", lambda _b: self.refresh_devices())
        grid.attach(self._refresh_button, 1, row, 1, 1)
        row += 1

        # Output folder ----------------------------------------------------
        grid.attach(self._label("Output folder"), 0, row, 1, 1)
        folder_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        folder_box.set_hexpand(True)
        self._folder_label = Gtk.Label(label=str(default_output_dir()))
        self._folder_label.set_ellipsize(3)  # PANGO_ELLIPSIZE_END
        self._folder_label.set_hexpand(True)
        self._folder_label.set_halign(Gtk.Align.START)
        self._folder_button = Gtk.Button(label="Choose…")
        self._folder_button.connect("clicked", self._on_choose_folder)
        folder_box.append(self._folder_label)
        folder_box.append(self._folder_button)
        grid.attach(folder_box, 1, row, 1, 1)
        row += 1

        # Status -----------------------------------------------------------
        self._status_label = Gtk.Label(label="Idle")
        self._status_label.set_halign(Gtk.Align.START)
        self._status_label.add_css_class("dim-label")
        root.append(self._status_label)

        self._elapsed_label = Gtk.Label(label="00:00")
        self._elapsed_label.add_css_class("title-1")
        self._elapsed_label.set_halign(Gtk.Align.START)
        root.append(self._elapsed_label)

        # Buttons ----------------------------------------------------------
        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._start_button = Gtk.Button(label="Start Recording")
        self._start_button.add_css_class("suggested-action")
        self._start_button.connect("clicked", self._on_start)
        self._stop_button = Gtk.Button(label="Stop Recording")
        self._stop_button.add_css_class("destructive-action")
        self._stop_button.connect("clicked", self._on_stop)
        buttons.append(self._start_button)
        buttons.append(self._stop_button)
        root.append(buttons)

        # Saved-file notice ------------------------------------------------
        self._saved_label = Gtk.Label()
        self._saved_label.set_halign(Gtk.Align.START)
        self._saved_label.set_wrap(True)
        self._saved_label.set_visible(False)
        root.append(self._saved_label)

        if self._diagnostics_enabled:
            hint = Gtk.Label(
                label="Diagnostics enabled; a summary is printed after each recording."
            )
            hint.add_css_class("dim-label")
            hint.set_halign(Gtk.Align.START)
            root.append(hint)

    @staticmethod
    def _label(text: str) -> Gtk.Label:
        label = Gtk.Label(label=text)
        label.set_halign(Gtk.Align.START)
        return label

    # -- device handling ----------------------------------------------------

    def refresh_devices(self) -> None:
        """Re-enumerate audio inputs, keeping the current choice if possible."""
        selected_name = self.selected_microphone()
        self._devices = list_input_devices()
        microphones = [device for device in self._devices if device.is_microphone]

        if microphones:
            self._mic_combo.set_model(Gtk.StringList.new([d.label for d in microphones]))
            index = 0
            if selected_name:
                for position, device in enumerate(microphones):
                    if device.name == selected_name:
                        index = position
                        break
            else:
                preferred = default_microphone(self._devices)
                if preferred is not None:
                    for position, device in enumerate(microphones):
                        if device.name == preferred.name:
                            index = position
                            break
            self._mic_combo.set_selected(index)
        else:
            self._mic_combo.set_model(Gtk.StringList.new(["No microphone found"]))
            self._mic_combo.set_selected(0)

        self._mic_combo.set_sensitive(bool(microphones))
        self._refresh_button.set_tooltip_text(
            f"{len(microphones)} microphone(s), "
            f"{len([d for d in self._devices if d.is_monitor])} system audio source(s)"
        )
        self._on_audio_changed()

    def selected_microphone(self) -> str | None:
        index = self._mic_combo.get_selected()
        if index == Gtk.INVALID_LIST_POSITION:
            return None
        microphones = [device for device in self._devices if device.is_microphone]
        if 0 <= index < len(microphones):
            return microphones[index].name
        return None

    def selected_audio_mode(self) -> AudioMode:
        index = self._audio_combo.get_selected()
        if 0 <= index < len(_AUDIO_MODE_LABELS):
            return _AUDIO_MODE_LABELS[index][1]
        return AudioMode.NONE

    def selected_recording_type(self) -> RecordingType:
        return (
            RecordingType.AUDIO
            if self._type_combo.get_selected() == 0
            else RecordingType.SCREEN
        )

    def selected_frame_rate(self) -> FrameRate:
        return FrameRate.FPS_15 if self._fps_combo.get_selected() == 0 else FrameRate.FPS_30

    def selected_output_dir(self) -> Path:
        return Path(self._folder_label.get_text())

    # -- widget signal handlers --------------------------------------------

    def _on_type_changed(self, *_args) -> None:
        audio_only = self.selected_recording_type() is RecordingType.AUDIO
        if audio_only:
            # Audio-only always records the microphone, so the four-way audio
            # selector does not apply.
            self._audio_combo.set_selected(
                [mode for _l, mode in _AUDIO_MODE_LABELS].index(AudioMode.MICROPHONE)
            )
        self._sync_sensitivity(self._recorder.state)

    def _on_audio_changed(self, *_args) -> None:
        self._sync_sensitivity(self._recorder.state)

    def _on_choose_folder(self, _button) -> None:
        dialog = Gtk.FileDialog()
        dialog.set_title("Choose where recordings are saved")

        def on_chosen(source, result) -> None:
            try:
                folder = source.select_folder_finish(result)
            except GLib.Error:
                return
            if folder is not None:
                self._folder_label.set_text(folder.get_path())

        dialog.select_folder(self, None, on_chosen)

    # -- recording ----------------------------------------------------------

    def _build_config(self) -> RecordingConfig:
        return RecordingConfig(
            recording_type=self.selected_recording_type(),
            frame_rate=self.selected_frame_rate(),
            audio_mode=self.selected_audio_mode(),
            microphone=self.selected_microphone(),
            output_dir=self.selected_output_dir(),
        )

    def _on_start(self, _button) -> None:
        self._saved_label.set_visible(False)
        try:
            config = self._build_config()
            config.validate()
        except ConfigError as exc:
            self._show_error(str(exc))
            return
        self._recorder.start(config)

    def _on_stop(self, _button) -> None:
        self._recorder.stop()

    # -- recorder callbacks -------------------------------------------------

    def _on_state_changed(self, state: RecorderState) -> None:
        messages = {
            RecorderState.IDLE: "Idle",
            RecorderState.STARTING: "Starting… (a screen sharing dialog may appear)",
            RecorderState.RECORDING: "Recording",
            RecorderState.STOPPING: "Finishing the file…",
            RecorderState.ERROR: "Stopped with an error",
        }
        self._status_label.set_text(messages.get(state, str(state)))
        self._sync_sensitivity(state)
        if state == RecorderState.RECORDING:
            self._start_timer()
        else:
            self._stop_timer()
        if state == RecorderState.IDLE:
            self._elapsed_label.set_text("00:00")

    def _sync_sensitivity(self, state: RecorderState) -> None:
        busy = state in (RecorderState.STARTING, RecorderState.RECORDING)
        starting_or_stopping = state in (
            RecorderState.STARTING,
            RecorderState.STOPPING,
        )
        recording = self.selected_recording_type() is RecordingType.SCREEN
        needs_mic = (
            self.selected_recording_type() is RecordingType.AUDIO
            or self.selected_audio_mode().wants_microphone
        )

        self._start_button.set_sensitive(not busy)
        self._stop_button.set_sensitive(busy or state == RecorderState.STOPPING)
        self._type_combo.set_sensitive(not busy)
        self._fps_combo.set_sensitive(not busy and recording)
        self._audio_combo.set_sensitive(
            not busy and self.selected_recording_type() is RecordingType.SCREEN
        )
        self._mic_combo.set_sensitive(
            not busy and needs_mic and bool(self.selected_microphone() or self._mic_combo.get_model())
        )
        self._refresh_button.set_sensitive(not busy)
        self._folder_button.set_sensitive(not starting_or_stopping)

        self._fps_label.set_sensitive(recording)
        self._audio_label.set_sensitive(
            self.selected_recording_type() is RecordingType.SCREEN
        )

    def _on_finished(self, path: Path) -> None:
        self._last_output = path
        self._saved_label.set_text(f"Saved: {path}")
        self._saved_label.set_visible(True)
        if self._diagnostics_enabled:
            # One summary per recording, never one line per frame.
            print(self._recorder.diagnostic_summary(), flush=True)

    def _on_error(self, message: str) -> None:
        self._show_error(message)
        if self._diagnostics_enabled:
            print(self._recorder.diagnostic_summary(), flush=True)

    def _show_error(self, message: str) -> None:
        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.CLOSE,
            text="Recording problem",
        )
        dialog.set_property("secondary-text", message)
        dialog.connect("response", lambda d, _r: d.destroy())
        dialog.present()

    # -- elapsed timer ------------------------------------------------------

    def _start_timer(self) -> None:
        self._stop_timer()
        self._timer_source = GLib.timeout_add(500, self._tick)

    def _stop_timer(self) -> None:
        if self._timer_source is not None:
            GLib.source_remove(self._timer_source)
            self._timer_source = None

    def _tick(self) -> bool:
        elapsed = int(self._recorder.elapsed_seconds)
        hours, remainder = divmod(elapsed, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours:
            text = f"{hours:d}:{minutes:02d}:{seconds:02d}"
        else:
            text = f"{minutes:02d}:{seconds:02d}"
        self._elapsed_label.set_text(text)
        return True

    # -- shutdown -----------------------------------------------------------

    def request_shutdown(self) -> None:
        """Called when the window closes, so no pipeline is left running."""
        self._stop_timer()
        self._recorder.shutdown()


class RecorderApplication(Gtk.Application):
    """GTK application wrapper. ``do_activate`` builds the single window."""

    def __init__(self) -> None:
        import os

        super().__init__(application_id="org.scientifica.TroikaDLite")
        self._window: RecorderWindow | None = None
        self._diagnostics_enabled = bool(os.environ.get(DIAGNOSTICS_ENV))

    def do_activate(self) -> None:
        if self._window is None:
            self._window = RecorderWindow(self, self._diagnostics_enabled)
            self._window.connect("close-request", self._on_close_request)
        self._window.present()

    def _on_close_request(self, _window) -> bool:
        if self._window is not None:
            self._window.request_shutdown()
        return False


def main(argv: list[str] | None = None) -> int:
    """Launch the GUI."""
    import os
    import sys

    app = RecorderApplication()
    return app.run(argv if argv is not None else sys.argv)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
