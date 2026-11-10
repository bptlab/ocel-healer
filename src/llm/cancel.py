# src/llm/cancel.py

"""Cooperative + immediate cancellation for LLM work.

A `CancelToken` is installed for the duration of a background job with
`use_token(token)`. Every LLM request made inside that block registers its
HTTP connection with the token (see `src.llm.client._request`), so
`token.cancel()` takes effect immediately:

* a request in flight is aborted by shutting down its socket — Ollama notices
  the disconnect and stops generating;
* any later request raises `Cancelled` before it is sent.

`Cancelled` derives from BaseException on purpose: code along the LLM path
uses broad `except Exception` blocks to degrade gracefully on LLM failures,
and a user abort must not be swallowed as "just another failed call".
Callers that want partial results (e.g. a sweep over many rows) catch
`Cancelled` explicitly.

Without an installed token (CLI, evaluation, tests) nothing changes.
"""

from __future__ import annotations

import socket
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator


class Cancelled(BaseException):
    """Raised inside a job when the user aborted it."""


class CancelToken:
    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._connections: set[Any] = set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise Cancelled()

    def cancel(self) -> None:
        """Request cancellation and abort every registered request now."""
        # Set the flag *before* looking at the sockets: a request that is
        # still connecting (sock is None) re-checks the flag right after
        # connecting, so it cannot slip through.
        self._event.set()
        with self._lock:
            conns = list(self._connections)
        for conn in conns:
            sock = getattr(conn, "sock", None)
            if sock is None:
                continue
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass  # already closed

    @contextmanager
    def track(self, conn: Any) -> Iterator[None]:
        """Register an http.client connection for the duration of a request."""
        self.raise_if_cancelled()
        with self._lock:
            self._connections.add(conn)
        try:
            yield
        finally:
            with self._lock:
                self._connections.discard(conn)


_current: ContextVar[CancelToken | None] = ContextVar("ocel_llm_cancel_token", default=None)


def current_token() -> CancelToken | None:
    """The token of the job running in this thread, or None."""
    return _current.get()


@contextmanager
def use_token(token: CancelToken) -> Iterator[CancelToken]:
    """Install `token` for all LLM calls made inside the block."""
    reset = _current.set(token)
    try:
        yield token
    finally:
        _current.reset(reset)
