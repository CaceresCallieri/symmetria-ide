"""The Qt lane in front of the (slow) per-harness thread readers.

`agent_threads`' readers walk a few hundred files and — from Phase 5 — spawn a
CLI. Neither may happen on the GUI thread, and neither may block startup, so
this owns the one worker that runs them and publishes each harness's rows the
moment that harness finishes. Claude does not wait for OpenCode.

Built on `GitOpsController`'s worker template, which is the shape this project
settled on for "a slow thing the UI asks for repeatedly": a request `Queue`, a
single `daemon=True` thread, and a `threading.Event` that `stop()` sets before
pushing a sentinel to unblock the blocking `get()` (project-standards §1 P0 —
daemon AND an explicit shutdown Event).

**Generations are the load-bearing part.** A project switch can arrive while a
read is in flight, and a scan started for the previous project will happily
return rows for it seconds later. Every request carries a generation counter;
its result is dropped unless the counter still matches when it comes back. The
same request also carries a per-generation cancel `Event`, set as soon as a
newer request arrives, so a cooperative reader returns early instead of holding
the queue. BOTH exist on purpose: cancellation is cooperative and therefore
advisory, while the generation check is what actually guarantees a stale row
never reaches the merge.

Results cross threads through `emit_gc_safe` (gotcha #10 — building the payload
allocates, and a Python 3.14 collection from a worker thread while
`QSGRenderThread` is mid-paint SEGVs), and every receiver connects with an
explicit `Qt.ConnectionType.QueuedConnection` (§4 P2) — see `connect_results`.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Sequence
from queue import Queue

from PySide6.QtCore import QObject, Qt, Signal

from .agent_bridge import emit_gc_safe
from .agent_threads import ThreadIndexReader, default_readers

log = logging.getLogger(__name__)

# Pushed by stop() to unblock the worker's blocking queue get().
_STOP = None

# The worker may be inside a reader when stop() lands; the join is bounded so a
# wedged reader delays shutdown rather than pinning it (the thread is daemon,
# so the process exits regardless).
_JOIN_TIMEOUT_SEC = 2.0


class AgentThreadIndexer(QObject):
    """Reads every registered harness's thread history, off the GUI thread.

    QML never touches this: `AppController` asks for a project via `start()`
    and merges what arrives on `threadsIndexed` into the rail's model.
    """

    # (project_root, harness, records) — emitted on the WORKER thread once a
    # single harness's read completes. Connect QUEUED (see connect_results).
    threadsIndexed = Signal(str, str, object)

    def __init__(
        self,
        readers: Sequence[ThreadIndexReader] | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        # Injectable so tests drive fakes and the suite never reads the
        # developer's real history; unset, the registry's readers are used.
        self._readers: list[ThreadIndexReader] = (
            list(readers) if readers is not None else default_readers()
        )
        self._queue: Queue = Queue()
        self._stop_event = threading.Event()
        # The CURRENT generation's cancel token. Replaced (not reused) on every
        # request, so setting the outgoing one can never cancel the incoming.
        self._cancel_event = threading.Event()
        # Guards `_generation` + `_cancel_event` against the worker reading
        # them while the GUI thread swaps them.
        self._lock = threading.Lock()
        self._generation = 0
        self._worker = threading.Thread(
            target=self._run_loop,
            daemon=True,
            name="agent-thread-index",
        )
        self._worker.start()

    # -- Wiring ------------------------------------------------------------

    def connect_results(self, handler: Callable[[str, str, object], None]) -> None:
        """Subscribe to finished reads, with the connection type spelled out.

        The emit happens on the index worker and `handler` touches GUI-thread
        state (the rail's model), so this is exactly the cross-thread case
        project-standards §4 P2 requires an explicit queued connection for.
        Offered as a method rather than left to callers so the connection type
        cannot be forgotten at one of several call sites.
        """
        # queued: index worker -> GUI thread (§4 P2)
        self.threadsIndexed.connect(handler, Qt.ConnectionType.QueuedConnection)

    # -- Requests ----------------------------------------------------------

    def start(self, project_root: str) -> None:
        """Index `project_root`, superseding whatever was in flight."""
        root = str(project_root or "")
        if not root or self._stop_event.is_set():
            return
        with self._lock:
            self._generation += 1
            generation = self._generation
            # Cancel the outgoing generation BEFORE publishing the new token,
            # so a reader that polls its event sees the switch immediately.
            self._cancel_event.set()
            cancel = threading.Event()
            self._cancel_event = cancel
        self._queue.put((generation, root, cancel))

    def stop(self) -> None:
        """Stop the worker: cancel, unblock the queue, join (bounded)."""
        self._stop_event.set()
        with self._lock:
            self._cancel_event.set()
        self._queue.put(_STOP)
        if self._worker.is_alive():
            self._worker.join(timeout=_JOIN_TIMEOUT_SEC)

    # -- Worker ------------------------------------------------------------

    def _is_current(self, generation: int) -> bool:
        with self._lock:
            return generation == self._generation

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            request = self._queue.get()
            if request is _STOP or self._stop_event.is_set():
                return
            generation, root, cancel = request
            for reader in self._readers:
                if self._stop_event.is_set() or not self._is_current(generation):
                    break
                self._read_one(reader, generation, root, cancel)

    def _read_one(
        self,
        reader: ThreadIndexReader,
        generation: int,
        root: str,
        cancel: threading.Event,
    ) -> None:
        harness = getattr(reader, "harness", "")
        try:
            records = list(reader.read(root, cancel))
        except Exception:  # noqa: BLE001 — one harness must not kill the lane
            # Blind on purpose: a reader shells out and parses foreign files,
            # so its failure modes are open-ended. Losing one harness's history
            # is a degraded rail; losing the worker is a rail that never
            # updates again for the rest of the session.
            log.exception("thread index: %s reader failed for %s", harness, root)
            return
        if not self._is_current(generation):
            # The project moved while this read ran. Dropping HERE — not
            # trusting the cancel event the reader was handed — is what makes
            # "a stale row can never reach the merge" true for a reader that
            # ignores cancellation entirely.
            log.debug(
                "thread index: discarding %d stale %s rows for %s",
                len(records),
                harness,
                root,
            )
            return
        # An EMPTY result is published too: it is how a harness with no history
        # left clears rows a previous scan published.
        emit_gc_safe(self.threadsIndexed, root, harness, records)
