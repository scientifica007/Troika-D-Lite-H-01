"""Tests for the interpreter selection helper.

``python3`` on ``PATH`` is frequently a pyenv, conda or ``/usr/local`` build
that cannot import the system ``gi`` bindings. The launchers re-exec themselves
with a system interpreter when that happens, so this logic decides whether the
application starts at all on a machine with more than one Python.
"""

from __future__ import annotations

import sys

from troika import interpreter


def test_candidate_interpreters_are_absolute_system_paths() -> None:
    for candidate in interpreter.CANDIDATE_INTERPRETERS:
        assert candidate.startswith("/usr/bin/python3")


def test_the_current_interpreter_is_used_when_it_has_gi() -> None:
    """A working interpreter must never be replaced."""
    original_execv = interpreter.os.execv
    calls = []

    def record(*args, **kwargs):
        calls.append(args)

    interpreter.os.execv = record
    try:
        interpreter.ensure_gi_capable_interpreter()
    finally:
        interpreter.os.execv = original_execv

    # ``gi`` is importable in the test environment, so no re-exec may happen.
    assert calls == []


def test_no_reexec_when_gi_is_missing_and_no_system_python_exists(monkeypatch) -> None:
    """Falling back must be silent; the caller prints the dependency message."""
    monkeypatch.setattr(interpreter, "_gi_available", lambda: False)
    monkeypatch.setattr(interpreter, "find_system_python", lambda: None)
    calls = []
    monkeypatch.setattr(interpreter.os, "execv", lambda *a, **k: calls.append(a))

    interpreter.ensure_gi_capable_interpreter()

    assert calls == []


def test_reexec_targets_the_system_interpreter(monkeypatch) -> None:
    monkeypatch.setattr(interpreter, "_gi_available", lambda: False)
    monkeypatch.setattr(interpreter, "find_system_python", lambda: "/usr/bin/python3")
    monkeypatch.setattr(sys, "executable", "/opt/conda/bin/python")
    calls = []

    def fake_execv(path, argv):
        calls.append((path, argv))

    monkeypatch.setattr(interpreter.os, "execv", fake_execv)

    interpreter.ensure_gi_capable_interpreter()

    assert calls, "an interpreter without gi must be replaced"
    path, argv = calls[0]
    assert path == "/usr/bin/python3"
    assert argv[0] == "/usr/bin/python3"
    # The original command line is preserved.
    assert argv[1:] == sys.argv


def test_no_reexec_when_already_running_the_system_interpreter(monkeypatch) -> None:
    """Re-execing into itself would loop forever."""
    monkeypatch.setattr(interpreter, "_gi_available", lambda: False)
    monkeypatch.setattr(interpreter, "find_system_python", lambda: "/usr/bin/python3")
    monkeypatch.setattr(sys, "executable", "/usr/bin/python3")
    calls = []
    monkeypatch.setattr(interpreter.os, "execv", lambda *a, **k: calls.append(a))

    interpreter.ensure_gi_capable_interpreter()

    assert calls == []


def test_has_gi_reports_false_for_a_missing_binary() -> None:
    assert interpreter._has_gi("/nonexistent/python3") is False


def test_find_system_python_returns_something_usable() -> None:
    """On a normal Ubuntu machine a gi-capable interpreter must be found."""
    found = interpreter.find_system_python()

    assert found is not None
    assert interpreter._has_gi(found)
