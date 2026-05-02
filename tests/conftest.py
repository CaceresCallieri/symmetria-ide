"""Shared pytest fixtures and helpers for the Symmetria IDE test suite.

Centralises infrastructure that would otherwise be duplicated across
structural (source-inspection) test modules.  Any test file that does
structural source checks or needs a bare QCoreApplication should import
from here rather than redefining these helpers locally.
"""

from __future__ import annotations

import inspect
import sys

import pytest
from PySide6.QtCore import QCoreApplication


@pytest.fixture(scope="session", autouse=True)
def qt_app():
    """Create a QCoreApplication for the session (required by Qt subsystems).

    QColor, QRectF, and QFontDatabase all abort without a QApplication
    present.  ``scope="session"`` keeps one instance alive for the whole
    run; ``autouse=True`` means tests that don't explicitly request it
    still benefit from it being initialised.
    """
    app = QCoreApplication.instance() or QCoreApplication(sys.argv)
    yield app


def construction_source(cls) -> str:
    """Return ``__init__`` source concatenated with every ``_init_*`` helper.

    NvimView's constructor is split across ``_init_buffers``,
    ``_init_springs``, and ``_init_signals`` helpers so that related
    initialisations are grouped together.  Tests verifying "a call/
    allocation exists during construction" must inspect that whole path,
    not just ``__init__``, otherwise they silently miss anything that
    lives in a helper.  This function stays resilient as new ``_init_*``
    helpers are added.
    """
    parts = [inspect.getsource(cls.__init__)]
    for name in dir(cls):
        if name.startswith("_init_"):
            member = getattr(cls, name)
            if callable(member):
                parts.append(inspect.getsource(member))
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Shared test doubles — used by test_app_controller_pool.py,
# test_app_controller_awaiting.py, and test_app_controller_permission_mode.py
# ---------------------------------------------------------------------------


class FakeSessionHost:
    """Stand-in for SessionHost — no threads, no subprocess, no network.

    Carries `instance_index` so tests can construct one fake per slot
    and assert dispatch routes to the right one. `is_running` is
    flipped True after `start()` to mimic the real host's spawn
    semantics — the cold-vs-hot branch of `submit_prompt_for` reads
    `is_running` to decide between `start(prompt)` and
    `send_user_message(prompt)`.

    Single canonical definition: define here, import in each test file
    that needs it. Adding a new capture field (e.g. Phase C's session_id)
    requires touching only this class, not N per-file copies.
    """

    def __init__(self, instance_index: int = 0) -> None:
        self.instance_index = instance_index
        self.is_running = False
        self.start_calls: list[str] = []
        self.send_calls: list[str] = []
        self.stop_calls = 0
        self.permission_calls: list[tuple[str, str]] = []
        self.set_permission_mode_calls: list[str] = []

    def start(self, prompt: str = "") -> None:
        self.start_calls.append(prompt)
        self.is_running = True

    def send_user_message(self, text: str) -> None:
        self.send_calls.append(text)

    def send_permission_response(self, request_id: str, behavior: str) -> None:
        self.permission_calls.append((request_id, behavior))

    def send_set_permission_mode(self, mode: str) -> None:
        self.set_permission_mode_calls.append(mode)

    def stop(self) -> None:
        self.stop_calls += 1
