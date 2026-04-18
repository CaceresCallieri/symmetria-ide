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
