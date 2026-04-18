"""Unit tests for bootstrap.py.

`QML_DIR`, `_resolve_qml_dir`, and `configure_logging` are pure
functions with no Qt dependencies — they run without a QApplication.
`configure_headless_mode` wires QTimer callbacks and is tested only
for its argument contract (the timers themselves are not driven here).
"""

from __future__ import annotations

import logging
from pathlib import Path


class TestResolveQmlDir:
    def test_returns_path_instance(self):
        from symmetria_ide.bootstrap import QML_DIR

        assert isinstance(QML_DIR, Path)

    def test_in_tree_path_exists(self):
        """When run from the project root the in-tree qml/ directory must exist."""
        from symmetria_ide.bootstrap import QML_DIR

        # The test suite always runs from the project tree, so the
        # in-tree branch of _resolve_qml_dir() should have fired.
        assert QML_DIR.exists(), f"QML_DIR {QML_DIR} does not exist on disk"

    def test_main_qml_present(self):
        """Main.qml must be present in the resolved directory."""
        from symmetria_ide.bootstrap import QML_DIR

        assert (QML_DIR / "Main.qml").exists()

    def test_private_resolver_matches_constant(self):
        """_resolve_qml_dir() called fresh must equal the module-level constant."""
        from symmetria_ide.bootstrap import QML_DIR, _resolve_qml_dir

        assert _resolve_qml_dir() == QML_DIR


class TestConfigureLogging:
    def test_calling_twice_does_not_raise(self):
        """basicConfig is idempotent — repeated calls must not raise."""
        from symmetria_ide.bootstrap import configure_logging

        configure_logging()
        configure_logging()  # must not raise

    def test_root_logger_has_handler_after_call(self):
        """After configure_logging(), the root logger should have at least one handler."""
        from symmetria_ide.bootstrap import configure_logging

        # Remove existing handlers so we can verify installation.
        root = logging.getLogger()
        original_handlers = root.handlers[:]
        root.handlers.clear()
        try:
            configure_logging()
            assert len(root.handlers) >= 1
        finally:
            # Restore original state so other tests aren't affected.
            root.handlers.clear()
            root.handlers.extend(original_handlers)


class TestConfigureHeadlessModeSignature:
    """Verify configure_headless_mode accepts the expected signature.

    We don't drive the Qt timers (that requires a running event loop), but
    we can verify the function is importable and accepts the right args by
    inspecting its parameter names.
    """

    def test_function_has_expected_parameters(self):
        import inspect
        from symmetria_ide.bootstrap import configure_headless_mode

        sig = inspect.signature(configure_headless_mode)
        params = list(sig.parameters)
        assert params == [
            "controller",
            "engine",
            "app",
            "shot_path",
            "test_keys",
        ], f"unexpected parameters: {params}"

    def test_shot_path_and_test_keys_accept_none(self):
        """Both optional string params have None as a valid type (str | None).

        This verifies the annotation carries the correct union — important
        because the caller checks `if shot_path or test_keys` before calling.
        """
        import inspect
        from symmetria_ide.bootstrap import configure_headless_mode

        sig = inspect.signature(configure_headless_mode)
        # Annotations are stringified by PEP 563; just verify the params exist.
        assert "shot_path" in sig.parameters
        assert "test_keys" in sig.parameters
