"""Troika D Lite — a small, reliable Wayland screen and audio recorder.

This file is the command-line entry point. It exists mainly to give a helpful
error when a GStreamer or GTK dependency is missing, instead of a traceback.
"""

from __future__ import annotations

import sys


_REQUIRED_MESSAGE = """Troika D Lite cannot start: {missing}

Install the dependencies with:

    sudo apt install python3-gi python3-gi-cairo python3-dbus \\
        gir1.2-gtk-4.0 gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 \\
        gstreamer1.0-tools gstreamer1.0-plugins-base \\
        gstreamer1.0-plugins-good gstreamer1.0-plugins-bad \\
        gstreamer1.0-plugins-ugly gstreamer1.0-pipewire

See README.md for the full setup instructions.
"""


def _check_imports() -> str | None:
    """Return a description of the first missing dependency, or ``None``."""
    try:
        import gi  # noqa: F401
    except ImportError:
        return "the Python GObject bindings (python3-gi)"

    try:
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk  # noqa: F401
    except (ImportError, ValueError):
        return "GTK 4 (gir1.2-gtk-4.0)"

    try:
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst

        Gst.init(None)
    except (ImportError, ValueError):
        return "GStreamer (python3-gi and the gstreamer1.0-* packages)"

    try:
        import dbus  # noqa: F401
    except ImportError:
        return "the Python D-Bus bindings (python3-dbus)"

    return None


def main(argv: list[str] | None = None) -> int:
    # A pip-installed console script can land on a pyenv/conda interpreter that
    # cannot import the system ``gi``; re-exec with a system one before the
    # dependency check so the user does not get a misleading "not installed".
    from .interpreter import ensure_gi_capable_interpreter

    ensure_gi_capable_interpreter()

    missing = _check_imports()
    if missing is not None:
        sys.stderr.write(_REQUIRED_MESSAGE.format(missing=missing))
        return 2

    from .ui import main as ui_main

    return ui_main(argv)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
