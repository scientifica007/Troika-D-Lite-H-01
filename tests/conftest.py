"""Shared test fixtures.

The tests deliberately avoid GTK and, where possible, avoid requiring a running
sound server: the logic worth testing is the selection, planning and state
machine logic, which is all pure Python.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


@pytest.fixture
def tmp_recording_dir(tmp_path: Path) -> Path:
    target = tmp_path / "recordings"
    target.mkdir()
    return target
