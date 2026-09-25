# Testing

Three layers: automated tests, a media self-test that runs without a desktop,
and a manual acceptance procedure on a real Wayland session.

Everything in this document was executed on the development machine unless it
is explicitly marked as a procedure for the reader to run.

## 1. Automated tests

```bash
python3 -m pytest tests/ -q
```

**Result on the development machine: 153 passed.**

| File | What it protects |
| --- | --- |
| `test_config.py` | Mode validation, derived flags, rejection of impossible combinations. |
| `test_filenames.py` | Timestamped names, collision suffixes, atomic path claiming, unwritable and full destinations. |
| `test_devices.py` | Microphone vs monitor classification, `pactl` fallback parsing, default-device choice. |
| `test_pipeline_plan.py` | The whole mode matrix: which sources, encoders, containers and clock roles each configuration produces. |
| `test_pipeline_build.py` | **Real GStreamer pipelines**: negotiated caps, frame-rate pinning, audio normalisation, stereo mixing, probe attachment, and error propagation. |
| `test_portal.py` | Portal request sequencing, cancellation, late/duplicate responses, session teardown. |
| `test_recorder.py` | State transitions, start/stop cycles, error recovery, shutdown, and that no pipeline survives a stop. |
| `test_diagnostics.py` | Gap detection, effective-FPS maths, and that disabled diagnostics collect nothing. |
| `test_selftest.py` | The self-test's own scenario and report logic. |

The tests deliberately avoid mocking the media layer where it matters.
`test_pipeline_build.py` builds genuine `Gst.Pipeline` objects with generated
sources and asserts on negotiated caps. Two real defects were found this way
that the planning tests could not see:

* the diagnostics probe was attached with a plain integer instead of
  `Gst.PadProbeType.BUFFER`, which raised `TypeError` the moment a recording
  with diagnostics enabled started;
* `audiomixer` collapsed a two-source mix to **mono**, because it derives its
  output channel count from downstream and nothing was asking for stereo. A
  system-audio + microphone recording therefore produced a mono track.

Both are now covered by regression tests that were confirmed to fail when the
fixes are reverted.

## 2. Media self-test

```bash
./bin/troika-selftest              # 6 seconds per scenario
./bin/troika-selftest -d 15        # longer scenarios
./bin/troika-selftest --keep       # keep the produced files
```

The self-test substitutes `videotestsrc` and `audiotestsrc` for the real
screen and audio devices, but it uses the **production** `build_pipeline`,
queue sizes, caps, encoder settings and stop sequence. Only the capture
elements are different. That means the parts most likely to break — queue
sizing, audio normalisation, two-source mixing, muxer finalisation, EOS
handling — are genuinely exercised.

Scenarios: video-only at 15 and 30 FPS, audio-only, a two-source audio mix with
deliberately mismatched rates and channel counts (44.1 kHz mono alongside
48 kHz stereo, as a real microphone and monitor produce), video + system audio
+ microphone, and repeated start/stop cycles. Each output is then re-parsed
with GStreamer's own discoverer, so the check reflects what a media player
sees rather than trusting that bytes were written.

**Result on the development machine: PASS**, all scenarios, with the produced
files reporting H.264 video and Opus audio at the expected frame rates.

This does not replace real Wayland testing. It cannot: it does not touch the
portal, the compositor, or a real microphone.

## 3. Verification performed with real sources

The development machine has a live Wayland session (`WAYLAND_DISPLAY=wayland-1`),
PipeWire, `xdg-desktop-portal-wlr`, GStreamer 1.26.2, and audio devices
(including a virtual microphone and a monitor source). The following were
executed through the **production** `Recorder` and pipeline code:

| Check | Result |
| --- | --- |
| Real microphone recording | 6.12 s, 307 buffers, **zero gaps**, clean state transitions, playable Ogg |
| Repeated start/stop, real microphone | 5 consecutive cycles, 5 playable files, no errors, no zombie pipeline |
| Stop finalisation latency | 5–40 ms per cycle |
| Screen + system audio + microphone, 30 FPS | video 30.0 FPS effective, audio 50 buffers/s, **zero gaps** in either stream, A/V durations 9.834 s vs 9.800 s |
| Screen + system audio, 15 FPS | video 15.0 FPS effective, **zero gaps**, playable |
| Screen + microphone, 15 FPS | video 15.0 FPS effective, **zero gaps**, playable |
| Two-minute screen + system audio + microphone, 30 FPS | 3608 video buffers, 6015 audio buffers, **zero gaps** in either stream, A/V durations 120.233 s vs 120.273 s, peak RSS 111.5 MB, ~18% of one CPU |
| Three-minute microphone-only recording | 180.60 s decoded, 9029 × 20 ms windows analysed, 1 silent window (0.0%), median RMS 18607, no audible gaps |
| Generated video + **real** system audio + **real** microphone, 30 FPS | 918 video buffers at 30.00 FPS, 1531 audio buffers at 50.01/s, **zero gaps** in either stream, A/V durations 30.6 s vs 30.6 s, stereo Opus, non-silent |
| Audio-only resource use | RSS steady at 42.1 MB across the run, ~1.2% of one CPU |
| Stop pressed during the permission dialog | cancels immediately, releases the session, returns to idle |
| Recorder reuse after that abort | starts again normally |
| Window closed mid-recording | file finalised and decodable; 11.8 s of a 12 s recording, no truncation |
| Portal negotiation with no user present | reaches the dialog and times out cleanly with a useful message |
| Error paths (unwritable folder, missing microphone, missing monitor) | each reports a clear error and leaves the recorder reusable |

The synthetic-video runs used a generated PipeWire video node in place of the
compositor's screen stream, because no interactive user was available to
approve the screen sharing dialog in the automated environment. The portal
negotiation itself was exercised separately and does reach the real dialog.

## 4. Manual acceptance procedure (real Wayland desktop)

Run these on the target machine. Record the results; do not assume they pass.

Set up diagnostics for every test:

```bash
TROIKA_DIAGNOSTICS=1 ./bin/troika-d-lite
```

The summary after each recording reports gaps directly. A test fails if
`gaps_over_threshold` is non-zero for a stream that should be continuous.

**Test A — microphone only.** Internal microphone, at least 3 minutes of
speech. Expect: no obvious gaps, no crackling, valid output.

**Test B — external microphone.** Plug in a USB microphone, press *Refresh
devices*, select it, record. Expect: the correct device is used, uninterrupted
recording, valid output.

**Test C — video only, 15 FPS.** Record the desktop while moving windows,
scrolling, and playing a local video. Expect: stable recording, no multi-second
freezes, valid output.

**Test D — video only, 30 FPS.** Repeat at 30 FPS.

**Test E — video + microphone.** At 15 FPS, and again at 30 FPS. Expect: smooth
video, continuous microphone, stable synchronisation.

**Test F — video + system audio.** Play music or a video while recording.
Expect: system audio captured, no repeated silence intervals.

**Test G — video + system audio + microphone.** Play audio *and* speak *and*
interact with the desktop. This is the most important test. Expect: both
sources present, no periodic audio interruptions, no multi-second video
freezes, no progressive drift.

**Test H — repeated start/stop.** At least five consecutive cycles. Expect:
every file playable, the application still usable, no zombie capture pipeline.
Confirm nothing is left running:

```bash
pgrep -af pipewiresrc
```

**Test I — long recording.** At least 20 minutes, preferably video + system
audio + microphone. Check for audio gaps, video gaps, A/V drift, growing memory
use, CPU instability, and corrupted finalisation. Watch memory with:

```bash
watch -n 5 'ps -o rss=,pcpu= -p $(pgrep -f troika-d-lite | head -1)'
```

### Checking the synchronisation of a finished recording

Compare the two stream durations. They should agree within a few tens of
milliseconds regardless of length:

```bash
gst-discoverer-1.0 recording.mkv | grep -E "Duration|video #|audio #"
```

A useful drift check is to record a clock with a visible second hand alongside
an audible tick and confirm they stay aligned to the end.

## 5. Performance measurement

Measure on the actual target machine; do not reuse numbers from another
machine.

```bash
TROIKA_DIAGNOSTICS=1 ./bin/troika-d-lite
# start a recording, then in another terminal:
watch -n 5 'ps -o rss=,pcpu=,etime= -p $(pgrep -f troika-d-lite | head -1)'
```

For each principal mode, record: average CPU, peak RSS, resolution, FPS,
encoder, duration, and whether the diagnostics reported any discontinuities.

### Measurements actually performed

On the development machine (software `x264enc`, no hardware acceleration),
1280×720, 30 FPS, system audio + microphone mixed, over a two-minute run:

| Metric | Value |
| --- | --- |
| Average CPU | ~18% of one core |
| Peak RSS | 111.5 MB |
| Resolution | 1280×720 |
| Frame rate | 30 FPS requested, 30.0 FPS effective |
| Encoder | `x264enc` (software) |
| Duration | 120 s |
| Audio gaps | 0 |
| Video gaps | 0 |
| File size | 5.3 MB |

### Expectations for the target hardware

These are **estimates, not measurements**. No old HP 630-class machine was
available for this work, so nothing here has been verified on the target.

The intended mitigation is that the design does not need the target machine to
be fast: software H.264 at 15 FPS is well within reach of a low-end dual-core
CPU, the video branch is allowed to drop frames rather than stall, and the
audio branches are isolated from video pressure. 15 FPS is the safer choice on
the oldest hardware; 30 FPS is expected to work on anything with a working
hardware encoder or a modest dual-core CPU.

If the target machine proves unable to sustain 30 FPS, that is a finding to
record, not a failure to hide. The 15 FPS mode exists for exactly that case.
