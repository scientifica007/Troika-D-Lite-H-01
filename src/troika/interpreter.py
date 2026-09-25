"""Find a Python interpreter that can import the GObject bindings.

``python3`` on ``PATH`` is not always the system interpreter. A pyenv, conda or
``/usr/local`` Python usually cannot see ``python3-gi``, which lives in the
distribution's ``dist-packages``. The launchers therefore check the interpreter
they were started with and re-exec themselves with a system one if needed.

This module is imported by the ``bin/`` launchers before the application is
loaded, so it must not import anything outside the standard library.
"""

from __future__ import annotations

import os
import sys
from shutil import which

#: Interpreters to try, in order, when the current one lacks ``gi``.
CANDIDATE_INTERPRETERS = (
    "/usr/bin/python3",
    "/usr/bin/python3.13",
    "/usr/bin/python3.12",
    "/usr/bin/python3.11",
    "/usr/bin/python3.10",
)


def _has_gi(executable: str) -> bool:
    """Return True if *executable* can import ``gi``."""
    import subprocess

    try:
        result = subprocess.run(
            [executable, "-c", "import gi"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def find_system_python() -> str | None:
    """Return an interpreter that has ``gi``, or ``None`` if there is not one."""
    seen: set[str] = set()
    for candidate in (which("python3"), *CANDIDATE_INTERPRETERS):
        if not candidate:
            continue
        real = os.path.realpath(candidate)
        if real in seen:
            continue
        seen.add(real)
        if os.path.exists(candidate) and _has_gi(candidate):
            return candidate
    return None


def _gi_available() -> bool:
    """Return True if ``gi`` can be imported by the running interpreter."""
    try:
        import gi  # noqa: F401
    except ImportError:
        return False
    return True


def ensure_gi_capable_interpreter() -> None:
    """Re-exec the current program with an interpreter that has ``gi``.

    Does nothing when the running interpreter is already suitable. If no
    suitable interpreter exists the caller carries on, so the normal dependency
    message is what the user sees rather than a confusing exec failure.
    """
    if _gi_available():
        return

    interpreter = find_system_python()
    if interpreter is None or os.path.realpath(interpreter) == os.path.realpath(sys.executable):
        return

    os.execv(interpreter, [interpreter, *sys.argv])
