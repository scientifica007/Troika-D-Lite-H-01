"""Troika D Lite — a small, reliable screen and audio recorder for Wayland.

The package is intentionally small. Each module owns one concern:

``modes``       recording type / frame rate / audio mode definitions
``config``      validated user configuration and its derived pipeline options
``filenames``   timestamped output path generation
``devices``     audio input enumeration (microphones and monitor sources)
``portal``      XDG Desktop Portal ScreenCast client (Wayland capture)
``pipeline``    GStreamer pipeline construction and element configuration
``diagnostics`` optional, low-overhead recording instrumentation
``recorder``    the recording state machine that ties everything together
``ui``          the single GTK4 window
``selftest``    a media self-test that runs without a desktop session
"""

__version__ = "1.0.0"
APP_NAME = "Troika D Lite"
APP_ID = "org.scientifica.TroikaDLite"
