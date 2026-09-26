"""Output file naming and destination checks.

Filenames are timestamped so a recording is never silently overwritten. The
container is Matroska (``.mkv``) for video and Ogg (``.ogg``) for audio-only.
Both are streamable containers that survive an interrupted write far better
than MP4, which keeps a central index that only becomes valid on finalisation.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from .config import ConfigError
from .modes import RecordingType

VIDEO_EXTENSION = "mkv"
AUDIO_EXTENSION = "ogg"

FILENAME_PREFIX = "Troika-D-Lite"


def extension_for(recording_type: RecordingType) -> str:
    return VIDEO_EXTENSION if recording_type is RecordingType.SCREEN else AUDIO_EXTENSION


def generate_filename(
    recording_type: RecordingType, when: datetime | None = None
) -> str:
    """Return a timestamped filename such as ``Troika-D-Lite_2026-09-25_18-30-00.mkv``."""
    stamp = (when or datetime.now()).strftime("%Y-%m-%d_%H-%M-%S")
    return f"{FILENAME_PREFIX}_{stamp}.{extension_for(recording_type)}"


def unique_output_path(
    output_dir: Path, recording_type: RecordingType, when: datetime | None = None
) -> Path:
    """Return a path in *output_dir* that does not exist yet.

    Timestamps have one-second resolution, so two recordings started within the
    same second would otherwise collide. A numeric suffix is appended until a
    free name is found, which guarantees we never overwrite a recording.
    """
    name = generate_filename(recording_type, when)
    candidate = output_dir / name
    if not candidate.exists():
        return candidate

    stem = candidate.stem
    suffix = candidate.suffix
    for counter in range(2, 1000):
        candidate = output_dir / f"{stem}_{counter}{suffix}"
        if not candidate.exists():
            return candidate
    raise ConfigError(f"Could not find an unused filename in {output_dir}")


def reserve_output_path(
    output_dir: Path, recording_type: RecordingType, when: datetime | None = None
) -> Path:
    """Claim an output path by creating it, so nobody else can take it.

    Timestamps are only accurate to the second. Creating the file atomically
    closes the window where a second recording started in the same second would
    compute the same name and overwrite the first. The file is created empty;
    the pipeline's file sink writes into it as soon as recording begins.

    Raises :class:`ConfigError` if no free name can be claimed.
    """
    stem = generate_filename(recording_type, when)
    base, _, suffix = stem.rpartition(".")
    for candidate_name in [stem] + [f"{base}_{n}.{suffix}" for n in range(2, 1000)]:
        candidate = output_dir / candidate_name
        try:
            # O_EXCL makes the check and the claim a single atomic operation.
            descriptor = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            continue
        except OSError as exc:
            raise ConfigError(f"Cannot create {candidate}: {exc}") from exc
        os.close(descriptor)
        return candidate
    raise ConfigError(f"Could not find an unused filename in {output_dir}")


def ensure_writable_directory(output_dir: Path) -> None:
    """Create *output_dir* if needed and confirm we can write a file into it.

    The probe writes a small file rather than only checking permission bits,
    because a read-only mount or a full disk passes a permission check but
    still fails the real operation.
    """
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ConfigError(f"Cannot create output folder {output_dir}: {exc}") from exc

    if not output_dir.is_dir():
        raise ConfigError(f"Output path is not a folder: {output_dir}")

    probe = output_dir / f".troika-write-test-{os.getpid()}"
    try:
        with open(probe, "wb") as handle:
            handle.write(b"")
        probe.unlink()
    except OSError as exc:
        raise ConfigError(f"Output folder is not writable: {output_dir} ({exc})") from exc


def check_free_space(output_dir: Path, minimum_bytes: int = 64 * 1024 * 1024) -> None:
    """Raise if the destination has less than *minimum_bytes* free.

    ``statvfs`` is unavailable on some filesystems; in that case the check is
    skipped rather than failing a recording that would probably have worked.
    """
    try:
        stats = os.statvfs(output_dir)
    except (OSError, AttributeError):
        return
    free = stats.f_bavail * stats.f_frsize
    if free < minimum_bytes:
        raise ConfigError(
            f"Not enough free space in {output_dir}: "
            f"{free // (1024 * 1024)} MiB available, "
            f"{minimum_bytes // (1024 * 1024)} MiB required."
        )
