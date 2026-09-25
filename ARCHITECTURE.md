# Architecture

Troika D Lite is a single-process GTK4 application that builds one GStreamer
pipeline per recording and drives it through a small explicit state machine.
There is no separate capture daemon, no helper process, no web server and no
GPU requirement.

## Why this stack

The brief fixes two constraints that decide almost everything else: the target
is a **Wayland** desktop, and the target hardware is **slow**. Those two facts
rule out most of the obvious alternatives:

* **FFmpeg with a screen-grab backend** would mean either an X11-only capture
  path (excluded by the brief) or hand-written PipeWire negotiation, plus manual
  management of the mixing, resampling and muxing that GStreamer already
  provides as tested elements.
* **A browser or Electron front end** is excluded outright, and would dwarf the
  encoder's own cost on the target hardware.
* **A hand-written PipeWire client** would be the smallest possible dependency
  set, but it means re-implementing format negotiation, resampling, clock
  slaving and muxing. That is a large amount of code whose failure modes are
  exactly the ones the brief asks to avoid: dropouts, drift and desync.

GStreamer is the one framework on Ubuntu that already solves the hard parts —
`pipewiresrc` for Wayland capture, `pulsesrc` for both microphones and monitor
sources, `audiomixer` for combining them, `audioresample` for differing clocks,
and `matroskamux`/`oggmux` for containers that survive an interrupted write.
GTK4 is the native toolkit on the target desktop and costs almost nothing at
idle. Both are in the Ubuntu archive, so the installed application needs no
network access.

The result is roughly 2,500 lines of Python across eleven focused modules.

## Process and threading model

One process, one GTK main loop, one pipeline per recording.

GStreamer runs the pipeline on its own threads; the application thread only
starts, stops and observes it. The application never polls the pipeline for
data, never reads frames, and never buffers media itself. Nothing but the
encoder's own queue holds audio or video, and nothing accumulates in Python
memory.

```
GTK main loop (main thread)
   |
   +-- Recorder (state machine)
   |      |
   |      +-- ScreenCastSession   -> D-Bus / XDG portal  (async callbacks)
   |      +-- Gst.Pipeline        -> PipeWire / PulseAudio capture threads
   |      +-- Diagnostics         -> pad probes, counters only
   |
   +-- RecorderWindow (widgets, elapsed-time timer)
```

## Modules

| Module | Responsibility |
| --- | --- |
| `modes.py` | The vocabulary: recording type, frame rate, audio mode. |
| `config.py` | `RecordingConfig` and its validation; no I/O beyond defaults. |
| `filenames.py` | Timestamped, collision-free output paths; destination checks. |
| `devices.py` | Audio input enumeration, separating microphones from monitor sources. |
| `portal.py` | XDG Desktop Portal ScreenCast client (D-Bus). |
| `pipeline.py` | `build_plan` (pure) and `build_pipeline` (real elements). |
| `diagnostics.py` | Optional counters and an end-of-recording summary. |
| `recorder.py` | The state machine tying all of the above together. |
| `ui.py` | The single GTK4 window. Holds no recording logic. |
| `selftest.py` | Runs the production pipeline with generated sources. |

The split between `build_plan` and `build_pipeline` is deliberate. `build_plan`
is a pure function from `RecordingConfig` to a `PipelinePlan`, so the entire
decision matrix — which sources, which encoders, which container, which caps —
is testable without a display server, a sound server or GStreamer itself.

## Screen capture path

Wayland does not allow an application to read the framebuffer directly. The
supported route is the **XDG Desktop Portal ScreenCast** interface, and that is
what this program uses:

1. `CreateSession` on `org.freedesktop.portal.ScreenCast`.
2. `SelectSources` with `types=MONITOR`, `multiple=false`,
   `cursor_mode=EMBEDDED` — one monitor, cursor drawn into the frame.
3. `Start`, which is what actually raises the compositor's permission dialog.
   The user picks the monitor.
4. `OpenPipeWireRemote`, which returns a file descriptor for the PipeWire
   remote.
5. That descriptor and the stream's node id are handed to `pipewiresrc`, which
   becomes the video source of the pipeline.

The dialog is a side effect of `Start` and is owned by the compositor, not by
this application. The program never asks for root and never touches
`/dev/dri` for capture.

Because the portal is asynchronous, `ScreenCastSession` is a small state
machine driven by D-Bus `Response` signals. Each request handle is registered
with a dispatcher that routes the response to the right callback, so a late or
duplicate response cannot re-enter a step that already completed.

`pipewiresrc` negotiates with the compositor and typically delivers the
compositor's own format (often `BGRx` at the monitor's resolution). A
`videoconvert` and a capsfilter pin the stream to `I420` before the encoder,
which keeps the H.264 output at standard 4:2:0 chroma rather than an unusual
profile, and makes the encoder's work predictable.

## Audio capture path

Both kinds of audio come from `pulsesrc`, which PipeWire serves through its
PulseAudio compatibility layer:

* a **microphone** is a normal capture source, chosen by name from the device
  list;
* **system audio** is the *monitor* of the default output device — the
  loopback of whatever the computer is playing.

Devices are enumerated through `Gst.DeviceMonitor`, with a `pactl` JSON parser
as a fallback for setups where the monitor does not report the devices the way
the monitor API expects. `AudioDevice` records whether each entry is a monitor,
so the distinction between "a thing you can speak into" and "a thing that
carries the speakers' output" is explicit rather than inferred from the name.

Each source gets its own branch:

```
pulsesrc -> audioconvert -> audioresample -> capsfilter -> queue
                                                   |
                                          (audiomixer, if two sources)
                                                   |
                                        audioconvert -> capsfilter -> opusenc
```

Every branch is normalised to the same format — 48 kHz, stereo, `S16LE`,
interleaved — *before* mixing. This is what makes a 44.1 kHz mono USB
microphone and a 48 kHz stereo monitor safe to combine: the mixer only ever has
to sum two streams that already agree on layout.

One subtlety that matters: `audiomixer` derives its output channel count from
downstream, and with nothing asking for stereo it collapses the mix to **mono**.
A capsfilter after the mixer therefore pins the mixed format explicitly. Without
it, a system-audio + microphone recording produced a mono track; the regression
is now covered by a test.

## Clocking and synchronisation

A pipeline with several live sources needs exactly one clock, or the streams
drift apart. `build_plan` marks the **first** audio source as the clock
provider and leaves the other slaved to it. When there is no audio at all, the
`pipewiresrc` stream provides the clock.

The microphone is chosen as the clock master when both audio sources are
present, because a real capture device has a hardware clock, while a monitor
source is a software loopback whose timing follows whatever is playing.

Timestamps are never rewritten. `matroskamux` and `oggmux` are given one
monotonic timeline from the sources and the muxer writes it through, which is
why the file's audio and video durations agree to within a few tens of
milliseconds even over two-minute recordings.

## Buffering and backpressure

The brief's hardest requirement is continuity, so the queueing is deliberate
rather than left at the defaults.

**Video is allowed to lose frames; audio is not.** This is the single most
important decision in the design.

* The video branch has a **leaky** queue of six buffers between the format
  conversion and the encoder. If the encoder falls behind, old video frames are
  dropped instead of the source being pushed back into. The timeline keeps
  moving, the compositor is never blocked, and pressure on the video branch can
  never propagate into the audio branches.
* Both audio branches have **non-leaky** queues sized in *time* (2 seconds),
  not in buffers, because a buffer count means a different duration at every
  sample rate. Audio is never discarded. The two seconds of slack absorb a
  scheduler hiccup or a slow first encoder frame without a dropout.

Sizing the audio queue in time rather than by buffer count is what makes the
design robust across devices with different chunk sizes. The latency this adds
is irrelevant for a recorder — this is not a monitoring application, and the
brief explicitly prefers a correct engineering trade-off over minimum latency.

`audiomixer` is given an extra 200 ms of input latency, so a momentarily late
branch cannot cause the other to be truncated.

## Encoder strategy

* **Video**: `x264enc` in `veryfast`-class settings tuned for old CPUs, with a
  modest bitrate. Software encoding is always available through
  `gstreamer1.0-plugins-ugly`, and it is what the self-test uses.
* **Hardware**: if `/dev/dri` exists *and* a VA-API, NVENC or V4L2 H.264
  encoder element is present, it is used instead. Hardware acceleration is an
  optimisation, never a requirement; when it is absent or unusable the software
  encoder is used and the recording proceeds normally. Presence of the element
  alone is not trusted — a VA-API encoder element exists on machines with no
  usable device — so `/dev/dri` is required as well.
* **Audio**: `opusenc`, 96 kbit/s alongside video and 128 kbit/s for
  audio-only. Opus is chosen for its quality per bit and its tolerance of lost
  or late input, which suits a recording that must survive scheduling jitter.

## Containers

* **Video recordings**: Matroska (`.mkv`), H.264 + Opus.
* **Audio-only recordings**: Ogg (`.ogg`), Opus.

Both are streamable containers that remain readable if the process is killed
mid-write, because they do not depend on a central index that only becomes
valid at finalisation. MP4 was rejected for exactly this reason: an MP4 whose
`moov` atom was never written is unplayable, and the brief is explicit that a
normal Stop must never produce an unreadable file.

Both are also playable in the players that ship with Ubuntu.

## Cleanup and finalisation

Stopping is the point where recorders usually break, so it is explicit:

1. EOS is sent to the live **sources**, not to the pipeline. Sending EOS to the
   pipeline would tear it down before the encoder had drained; sending it to
   the sources lets each one finish the buffer it is working on and push EOS
   downstream.
2. The pipeline drains and the muxer writes its index and trailer.
3. A finish timer guards the case where the drain never completes, so the UI
   cannot hang in "Finishing the file…".
4. The pipeline is set to `NULL` and the portal session and PipeWire fd are
   released.
5. The UI returns to idle and shows the saved path.

Measured on the test machine, a normal stop finalises in **5–40 ms**.

If the pipeline fails mid-recording, the file written so far is still
finalised and its path is reported, so a device disappearing cannot silently
lose several minutes of video.

Pressing Stop while the permission dialog is still open aborts immediately:
the session is closed, which asks the portal to dismiss the dialog, and the
callbacks are dropped so a late response cannot start a recording the user
already cancelled. The recorder returns to idle and is immediately reusable.

## Diagnostics

Diagnostics are off unless `TROIKA_DIAGNOSTICS=1` is set. When they are off, no
pad probes are attached at all — the normal recording path pays nothing.

When on, a probe sits on the last queue of each branch, so it measures exactly
what the container receives, after encoding. Each probe increments a counter and
subtracts timestamps; there is no per-frame formatting and no terminal output
until the recording ends. The summary reports buffers, effective frame rate,
the largest timestamp gap, and a count of gaps above a visibility/audibility
threshold — a gap count is far more informative than an average, because a
recording can have excellent average throughput and still contain a two-second
freeze.

## CPU and memory rationale

* No preview. Decoding and rendering the capture stream back to the user would
  roughly double the cost of recording.
* No per-frame processing in Python. Frames never enter the application.
* Media streams straight through to the file; the application never holds a
  recording in RAM.
* The UI is a single window with a 500 ms elapsed-time timer and no animation.
* Nothing polls. The portal is event-driven through D-Bus signals; the pipeline
  is event-driven through bus messages.
* Logging is silent during normal operation; diagnostics are opt-in and produce
  one summary per recording.

Measured on the development machine (software `x264enc`, 1280×720, 30 FPS,
system audio + microphone): about **18% of one CPU**, peak RSS about **110 MB**,
and a two-minute recording with **zero** audio or video gaps in either stream.
These are measurements from this machine, not from the target old laptop; see
`TESTING.md` for the exact conditions.
