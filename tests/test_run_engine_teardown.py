"""Post-`app.exec()` teardown ordering (`_finalize_qml_engine`).

Destroying the QML engine runs the KSession dtors (editor nvim, shell, every
agent pane) and releases the WebEngine SingletonLock. The reload path REQUIRES
it; on a normal quit it exists so the dtors run at a known point rather than at
whatever ordering interpreter finalization produces. See
`_finalize_qml_engine`'s docstring for the 2026-08-05 measurement table — in
particular, an agent CLI outliving the IDE is the tmux substrate working as
designed, NOT something this call fixes.

`shiboken6.delete` on a real engine needs a live QGuiApplication and cannot be
exercised here, which is why the delete sits behind `_destroy_engine` — these
tests substitute it and pin the two things a refactor can silently break: that
the destruction happens AT ALL on a normal quit, and that the reload argv/env
are captured BEFORE it (they read controller state that must not be touched
post-teardown).
"""

from __future__ import annotations

import ast
import inspect

import pytest

from symmetria_ide import app as app_module


class _FakeController:
    """Only the two attributes `_finalize_qml_engine` reads.

    `displayedRoot` records its read into the shared trace so the
    capture-before-destroy ordering is observable rather than assumed.
    """

    def __init__(self, trace: list[str], *, reload_requested: bool) -> None:
        self.reload_requested = reload_requested
        self._trace = trace

    @property
    def displayedRoot(self) -> str:  # noqa: N802 — mirrors the Qt property name
        self._trace.append("read-displayedRoot")
        return "/home/jc/projects/example"


@pytest.fixture
def trace(monkeypatch) -> list[str]:
    events: list[str] = []
    monkeypatch.setattr(
        app_module, "_destroy_engine", lambda engine: events.append("destroy")
    )
    monkeypatch.setattr(app_module, "_reload_env", lambda: {"SYMMETRIA": "1"})
    return events


def test_normal_quit_still_destroys_the_engine(trace):
    """No reload latch, engine still torn down — so KSession/WebEngine dtors
    run at a known point instead of at interpreter-finalization ordering."""
    controller = _FakeController(trace, reload_requested=False)
    assert app_module._finalize_qml_engine(object(), controller) is None
    assert trace == ["destroy"]


def test_reload_captures_argv_and_env_before_destroying(trace):
    controller = _FakeController(trace, reload_requested=True)
    plan = app_module._finalize_qml_engine(object(), controller)
    assert plan is not None
    argv, env = plan
    assert argv[1:] == ["-m", "symmetria_ide", "/home/jc/projects/example"]
    assert env == {"SYMMETRIA": "1"}
    # Ordering, not just presence: the controller read must precede the dtor.
    assert trace == ["read-displayedRoot", "destroy"]


def test_run_finalizes_unconditionally():
    """`run()` must finalize after every event-loop exit, not only reloads.

    The direct tests above pin the finalizer's behavior. This source-level seam
    only guards its placement in `run()`: a behavior-preserving rewrite of the
    assignment is allowed, while putting the call under a reload-dependent
    branch is not.
    """
    tree = ast.parse(inspect.getsource(app_module))
    run_fn = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "run"
    )
    calls = [
        node
        for node in ast.walk(run_fn)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", "") == "_finalize_qml_engine"
    ]
    assert len(calls) == 1

    call = calls[0]
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(run_fn):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    ancestors: list[ast.AST] = []
    node: ast.AST = call
    while node in parents:
        node = parents[node]
        ancestors.append(node)
        if node is run_fn:
            break

    enclosing_ifs = [node for node in ancestors if isinstance(node, ast.If)]
    guarded_by_reload = [
        node
        for node in enclosing_ifs
        if "reload_requested" in ast.unparse(node.test)
        or "reload_plan" in ast.unparse(node.test)
    ]
    assert guarded_by_reload == []
