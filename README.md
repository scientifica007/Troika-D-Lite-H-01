# Troika D Lite

A small, reliable screen and audio recorder for Ubuntu 24.04 LTS and newer,
built for Wayland desktops and for hardware that is not fast.

Troika D Lite does one thing: it records the screen, the microphone, the
computer's own audio, or a combination of them, and it writes a file that
plays. It has no preview, no editing, no streaming, no scenes and no accounts,
because every one of those would cost CPU that a low-powered laptop does not
have to spare.

## What it records

| Recording type | Audio choice | Result |
| --- | --- | --- |
| Audio only | microphone | `.ogg` (Opus) |
| Screen recording | no audio | `.mkv` (H.264) |
| Screen recording | system audio | `.mkv` (H.264 + Opus) |
| Screen recording | microphone | `.mkv` (H.264 + Opus) |
| Screen recording | system audio + microphone | `.mkv` (H.264 + Opus, mixed) |

Screen recordings can run at **15 FPS** or **30 FPS**. Both are selected before
recording starts.

## Requirements

* Ubuntu 24.04 LTS or newer, on a **Wayland** session.
* A working PipeWire and `xdg-desktop-portal` setup (both are default on
  Ubuntu 24.04).
* Python 3.10 or newer with the system GObject bindings.

Everything is installed from the standard Ubuntu archive. There are no Python
packages to download, so the recorder keeps working offline after installation.

## Installation

```bash
git clone https://github.com/scientifica007/Troika-D-Lite-H-01.git
cd Troika-D-Lite-H-01
./scripts/install.sh
```

The script installs the runtime packages with `apt` and puts `troika-d-lite`
in `~/.local/bin`. If that directory is not on your `PATH`, the script prints
the two lines needed to add it.

To install only the packages, without the launcher:

```bash
./scripts/install.sh --deps
```

To remove the launcher later:

```bash
./scripts/install.sh --uninstall
```

### Running without installing

The launchers work straight from a checkout, so this is enough to try it:

```bash
sudo apt install python3-gi python3-gi-cairo python3-dbus gir1.2-gtk-4.0 \
    gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly gstreamer1.0-pipewire

./bin/troika-d-lite
```

## Using it

1. Choose **Audio only** or **Screen recording**.
2. For screen recordings, choose **15 FPS** or **30 FPS**.
3. Choose the audio you want: none, system audio, microphone, or both.
4. If a microphone is needed, pick it from the list. Press **Refresh devices**
   after plugging in a USB microphone.
5. Choose an output folder, or keep the default `~/Videos`.
6. Press **Start Recording**. For screen recordings the normal Wayland screen
   sharing dialog appears; pick the monitor you want to share.
7. Press **Stop Recording**. The status line changes to "Finishing the file..."
   while the encoder drains and the container is finalised. When it is done,
   the full path of the saved file is shown in the window.

Files are named after the time they started, for example
`Troika-D-Lite_2026-09-25_18-30-00.mkv`. An existing recording is never
overwritten; if two recordings start in the same second, a numeric suffix is
added.

## Diagnostics

Set `TROIKA_DIAGNOSTICS=1` to print a short summary after each recording:

```bash
TROIKA_DIAGNOSTICS=1 troika-d-lite
```

The summary reports the chosen sources, negotiated formats, effective frame
rate, encoder, and any timestamp gaps that were large enough to be visible or
audible. Diagnostics cost nothing when they are switched off: no probes are
attached at all.

## Self-test

The media architecture can be exercised without a desktop session, because the
self-test substitutes generated video and audio for the real capture sources
while using the production pipeline code:

```bash
./bin/troika-selftest
```

It records video-only at 15 and 30 FPS, audio-only, a two-source audio mix, and
a full video + system audio + microphone recording, then verifies each file
with GStreamer's own parser. It also runs repeated start/stop cycles. This
complements real Wayland testing; it does not replace it. See `TESTING.md`.

## Troubleshooting

**The screen sharing dialog never appears.**
Check that a portal backend is running:

```bash
systemctl --user status xdg-desktop-portal
systemctl --user status xdg-desktop-portal-gnome   # or -kde, -wlr, -hyprland
```

If the backend is missing, install the one matching your desktop, for example
`sudo apt install xdg-desktop-portal-gnome` on Ubuntu.

**"The desktop portal does not provide ScreenCast."**
Your portal backend does not implement screen casting. Install the backend that
matches your compositor; `xdg-desktop-portal-wlr` is the one for wlroots-based
compositors such as Sway.

**No system audio is recorded.**
System audio is captured from the *monitor* of your default output device.
Confirm that one exists:

```bash
pactl list short sources | grep -i monitor
```

If nothing is listed, make sure PipeWire and WirePlumber are running and that
you are not on a session where the monitor is deliberately hidden.

**A USB microphone does not appear.**
Press **Refresh devices** after plugging it in. Devices are enumerated on
demand, so no restart is needed.

**The recording starts but the file is silent.**
Check the microphone that is actually selected in the dropdown. On machines
with several inputs it is easy to leave a disconnected one selected.

**A recording stopped by itself.**
If the capture device disappears or the pipeline fails, the file written so far
is still finalised and its path is reported, so a partial recording is not lost.

## Documentation

* [`ARCHITECTURE.md`](ARCHITECTURE.md) - how the capture, encoding and
  synchronisation are put together, and why.
* [`TESTING.md`](TESTING.md) - automated tests, the self-test, and the manual
  acceptance procedure on a real Wayland desktop.
* [`KNOWN_LIMITATIONS.md`](KNOWN_LIMITATIONS.md) - what the program does not do.

## Uninstalling

```bash
./scripts/install.sh --uninstall
```

This removes the launcher and the desktop entry. Your recordings and the
packages installed by `--deps` are left alone.

## License

MIT. See `pyproject.toml`.
