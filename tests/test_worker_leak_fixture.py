"""The autouse teardown that stops leaked AppControllers must not go stale.

WHY THIS FILE EXISTS. `conftest._release_app_controller_workers` names the
worker-owning sub-controllers in a hardcoded tuple. Add a sixth one — or rename
an existing one — and the fixture keeps passing while silently stopping less
than it claims, which puts the suite straight back on the ramp it was built to
end: 230 live `AppController`s, 1159 threads and 224 inotify instances mid-run,
against a system budget shared with the developer's desktop.

The failure mode is what makes this worth a test rather than a comment. Nothing
breaks at the moment of the drift. It resurfaces months later as "the suite
died again", in a file that has nothing to do with the change that caused it,
and the last person to edit anything gets the blame — which is exactly how the
original took three sessions to pin down.
"""

from __future__ import annotations

import re

import conftest


def _shutdown_stop_targets() -> set[str]:
    """Attributes `AppController.shutdown` calls `.stop()` on.

    Read from source rather than by calling it: `shutdown()` saves the session,
    quits nvim over RPC and tears down Chrome, none of which a test may do.
    """
    import inspect

    from symmetria_ide.app import AppController

    source = inspect.getsource(AppController.shutdown)
    return set(re.findall(r"self\.(_\w+)\.stop\(\)", source))


# Sub-controllers `shutdown()` stops that the fixture deliberately does NOT.
# Every entry needs a reason, because the default answer is "add it to the
# fixture" — an exclusion is a claim that the thing owns no thread pinning its
# controller.
_DELIBERATELY_EXCLUDED = {
    "_backend": "nvim RPC — sends `qa!`, which would kill an editor a test never started",
    "_agent_bridge": "socket client; a test that starts one owns stopping it",
    "_agent_events": "same — and unlinks a socket path the test chose",
    "_account_usage_store": "no worker thread pinning the controller",
    "_browser_mcp_server": "daemon thread, reaped at interpreter exit",
    "_chrome_host": "terminates a Chrome the suite never spawns (fixture-blocked)",
}


def test_the_fixture_covers_every_worker_owning_subcontroller():
    """Each `.stop()` target in `shutdown()` is either released by the fixture
    or explicitly excused above — no third, silent category."""
    covered = set(conftest._WORKER_OWNING_SUBCONTROLLERS)
    unaccounted = _shutdown_stop_targets() - covered - set(_DELIBERATELY_EXCLUDED)
    assert not unaccounted, (
        "AppController.shutdown() stops these, but the teardown fixture neither "
        "stops nor excuses them — if any owns a worker thread, its controller is "
        "pinned for the whole run:\n  " + "\n  ".join(sorted(unaccounted))
    )


def test_every_named_subcontroller_still_exists():
    """Guards the other direction: a RENAME leaves the fixture's `getattr`
    silently finding nothing, so it would stop four of five and still pass."""
    from symmetria_ide.app import AppController

    source = _shutdown_stop_targets()
    missing = [
        name for name in conftest._WORKER_OWNING_SUBCONTROLLERS if name not in source
    ]
    assert not missing, (
        f"named in the fixture but never stopped by {AppController.__name__}."
        f"shutdown() — renamed or removed: {missing}"
    )
