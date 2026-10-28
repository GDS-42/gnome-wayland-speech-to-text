"""Background execution helpers bridging threads and the GLib main loop."""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable
from typing import Any

from gi.repository import GLib

log = logging.getLogger(__name__)


def call_in_main(fn: Callable[..., Any], *args: Any) -> None:
    """Run ``fn(*args)`` on the GLib main loop (thread-safe)."""

    def trampoline() -> bool:
        try:
            fn(*args)
        except Exception:  # never let an exception kill the main loop source
            log.exception("main-loop callback %r failed", fn)
        return GLib.SOURCE_REMOVE

    GLib.idle_add(trampoline)


class Worker:
    """A single thread running jobs in FIFO order.

    The engine is not thread-safe and model loading must happen before the
    transcriptions submitted after it, so all engine work goes through one
    ordered queue. ``on_done``/``on_error`` are invoked on the main loop.
    """

    def __init__(self, name: str) -> None:
        self._queue: queue.Queue[tuple | None] = queue.Queue()
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._thread.start()

    def submit(
        self,
        job: Callable[[], Any],
        on_done: Callable[[Any], None] | None = None,
        on_error: Callable[[BaseException], None] | None = None,
    ) -> None:
        self._queue.put((job, on_done, on_error))

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            job, on_done, on_error = item
            try:
                result = job()
            except Exception as exc:
                log.debug("job %r failed: %s", job, exc, exc_info=True)
                if on_error is not None:
                    call_in_main(on_error, exc)
                else:
                    log.exception("unhandled error in background job")
            else:
                if on_done is not None:
                    call_in_main(on_done, result)

    def shutdown(self, timeout: float = 3.0) -> None:
        self._queue.put(None)
        self._thread.join(timeout)
