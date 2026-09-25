# Known limitations

Only real limitations are listed here. Every mandatory recording mode in the
brief is implemented; nothing below is a mandatory feature disguised as a
limitation.

## Capture

* **One monitor per recording.** The portal request uses `multiple=false`, so
  the user selects a single monitor in the Wayland dialog. Recording two
  monitors side by side in one file is not supported. Capturing the full
  desktop across a multi-monitor setup is therefore done by recording one
  monitor at a time.
* **No window or region capture.** Only full monitors. The brief asks for the
  complete selected desktop, and that is what is implemented; window and region
  selection are deliberately out of scope.
* **The cursor is always drawn into the frame.** The portal request uses
  `cursor_mode=EMBEDDED`. There is no option to record without the pointer.
* **X11 sessions are not supported by the capture path.** The implementation is
  built around the XDG Desktop Portal ScreenCast interface and PipeWire. An X11
  session with a portal backend that provides ScreenCast will work; a bare X11
  session without one will not. The brief makes Wayland first-class and X11
  optional, and the optional path was not implemented rather than implemented
  badly.
* **The compositor chooses the capture format and size.** `pipewiresrc`
  negotiates with the compositor, so the resolution is whatever the monitor
  provides. There is no downscaling option, which is intentional: rescaling
  costs CPU that the target hardware does not have.

## Audio

* **System audio depends on a monitor source existing.** System audio is the
  monitor of the default output device. If the session or the output device
  does not expose one, system audio cannot be recorded and the application says
  so rather than recording silence. This is a property of the sound server
  configuration, not something the recorder can work around.
* **The mix is a plain sum with no per-source gain.** System audio and
  microphone are combined by `audiomixer` at equal weight. There are no
  separate volume sliders, so if one source is much louder than the other the
  balance has to be corrected at the source.
* **No live level meters.** There is no preview of the audio being captured,
  because that would mean tapping and rendering the audio stream that the brief
  asks to keep cheap.
* **Hot-plug detection is manual.** A microphone connected while the
  application is running appears after pressing *Refresh devices*. The brief
  explicitly permits this. Device enumeration is also not refreshed
  automatically when the dialog is opened.

## Features intentionally absent

These are excluded by the brief, and are listed only so their absence is not
mistaken for an oversight: no streaming, no webcam, no editing, no
annotations, no overlays, no scene management, no effects, no cloud functions
and no accounts. There is also no pause/resume, because it would require
rewriting timestamps on both audio and video branches — a meaningful risk to
continuity for a feature the brief does not ask for.

## Configuration

* **Encoder settings are not exposed.** Preset, bitrate, keyframe interval and
  encoder choice are fixed internal defaults. The brief asks for a small
  interface with sensible defaults, and these defaults are tuned for old CPUs.
* **The container is not selectable.** Video goes to Matroska and audio-only to
  Ogg. Both were chosen for reliable finalisation, which matters more here than
  the container preference of a particular player.
* **Only 15 and 30 FPS.** Both required rates are supported; other rates are
  not offered.

## Hardware encoding

Hardware H.264 encoding is used when `/dev/dri` exists and a VA-API, NVENC or
V4L2 encoder element is present, but this has **not been verified on real
hardware-accelerated machines** during development — the development machine
has no usable VA-API device and always took the software path. The detection is
deliberately conservative and falls back to software encoding, so the failure
mode is a slower recording rather than a broken one. On some systems a VA-API
element exists but fails at runtime; in that case the pipeline reports an error
and the recording does not start, which is the honest outcome rather than a
silent quality drop.

## Verification gaps

* **No testing on the target hardware class.** No old HP 630-class machine was
  available. All CPU and memory figures in `TESTING.md` come from the
  development machine and are labelled as such. The estimates for low-end
  hardware are estimates.
* **The longest recording actually run was two minutes.** The 20-minute
  acceptance test is documented as a procedure but was not executed here. Two
  minutes is long enough to expose the common drift and gap failures, but it is
  not evidence about long-run memory growth.
* **No interactive screen sharing approval was performed end to end.** The
  automated environment had no user to click the compositor's dialog. The
  portal negotiation was exercised as far as raising that dialog, and the
  video pipeline was exercised with a generated PipeWire video node in place of
  the compositor's stream. The first thing a human should do is run Test C from
  `TESTING.md` on a real desktop.
* **Real microphone and system-audio capture was verified**, including a
  two-minute mixed recording with zero gaps, but only on the development
  machine's devices.
