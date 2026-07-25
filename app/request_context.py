"""Ambient request identifier for code far below the HTTP layer.

The provider client and the audit sink run several layers beneath the route,
behind a long-lived `FMPClient` that is shared by every request, so the
per-request id cannot be threaded through their signatures without dragging an
HTTP concern through the cache and normalization layers. A context variable
keeps it available where evidence is recorded (Phase 10) and where structured
logs will need it (Phase 11), while staying `None` for callers that have no
request at all.

Each ASGI request runs in its own task context, so a plain `set()` from the
request-id middleware is scoped to that request. `request_id_scope` exists for
non-HTTP entry points (the daily refresh run) that must restore the previous
value when they finish.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_REQUEST_ID: ContextVar[str | None] = ContextVar("pubtools_request_id", default=None)


def current_request_id() -> str | None:
    """The active request/run identifier, or None outside any request."""
    return _REQUEST_ID.get()


def set_request_id(request_id: str | None) -> None:
    """Bind the identifier for the remainder of this task's context."""
    _REQUEST_ID.set(request_id)


@contextmanager
def request_id_scope(request_id: str | None) -> Iterator[None]:
    """Bind an identifier for the duration of the block, then restore it."""
    token = _REQUEST_ID.set(request_id)
    try:
        yield
    finally:
        _REQUEST_ID.reset(token)
