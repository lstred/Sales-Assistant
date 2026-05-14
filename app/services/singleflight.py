"""Tiny singleflight helper: collapse concurrent identical calls into one.

When multiple background threads ask for the same (expensive) result at the
same time, only the *first* caller actually does the work. The other callers
block on a shared :class:`threading.Event` and receive the same return value
once it is ready.

This is exactly what we need for the cross-view "refresh all" path: four
:class:`SalesFilterBar` instances would otherwise fire four identical
``load_blended_sales`` calls in parallel, hammering SQL Server with the
same query four times. With singleflight, only one query runs and the
other three wait on it.
"""

from __future__ import annotations

import threading
from typing import Callable, TypeVar

T = TypeVar("T")


class _Pending:
    __slots__ = ("event", "value", "error")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.value: object = None
        self.error: BaseException | None = None


class SingleFlight:
    """Thread-safe deduper keyed by an arbitrary hashable key."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._inflight: dict[object, _Pending] = {}

    def do(self, key: object, fn: Callable[[], T]) -> T:
        # Fast path: register or join the in-flight slot.
        with self._lock:
            pending = self._inflight.get(key)
            owner = pending is None
            if owner:
                pending = _Pending()
                self._inflight[key] = pending

        if owner:
            try:
                pending.value = fn()
            except BaseException as exc:  # noqa: BLE001 — re-raised below
                pending.error = exc
            finally:
                with self._lock:
                    self._inflight.pop(key, None)
                pending.event.set()
        else:
            pending.event.wait()

        if pending.error is not None:
            raise pending.error  # type: ignore[misc]
        return pending.value  # type: ignore[return-value]


# Module-level singleton shared by all callers in the process.
sales_singleflight = SingleFlight()
