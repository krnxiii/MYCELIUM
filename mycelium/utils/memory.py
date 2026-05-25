"""Periodic glibc heap trim — return freed arena pages to the OS.

Long-running ingest/extraction/tend bursts allocate large transients (networkx
graph load in core/community, embedding lists in core/export, the 32 MB
subprocess buffer in llm/client). glibc frees them inside the process but does
NOT return the pages to the OS: RSS pins to the transient high-water and never
falls. Measured on the prod workload (glibc 2.41, identical churn):

    glibc default        : RSS 14 -> 994 MB, 785 MB freed-but-retained
    glibc + malloc_trim  : RSS 14 -> 284 MB,  75 MB retained   (working set)
    jemalloc default     : RSS 26 -> 1101 MB, worse (lazy decay holds dirty)

Over 18 days this pinned prod RSS at 5.6 GB while the real working set was
~150 MB. malloc_trim(0) madvise-releases free pages across the whole heap.
The automatic glibc knob (MALLOC_TRIM_THRESHOLD_) only trims the contiguous
top chunk and does not reclaim mid-heap fragmentation, so an explicit periodic
trim is required.

No-op on non-glibc platforms (macOS dev, musl) — graceful degradation.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import os
import threading
import time

import structlog

log = structlog.get_logger()

_DEFAULT_INTERVAL_S = 60.0
_ENV_INTERVAL       = "MYCELIUM_MALLOC_TRIM_INTERVAL"  # seconds; 0 disables


def _load_libc() -> ctypes.CDLL | None:
    """Return a libc handle iff it exposes malloc_trim (glibc), else None."""
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6",
                           use_errno=True)
    except OSError:
        return None
    if not hasattr(libc, "malloc_trim"):
        return None
    libc.malloc_trim.argtypes = [ctypes.c_size_t]
    libc.malloc_trim.restype  = ctypes.c_int
    return libc


_libc = _load_libc()


def malloc_trim() -> bool:
    """Release free heap pages back to the OS. True if memory was released.

    Thread-safe (takes the glibc arena lock). No-op (False) off glibc.
    """
    if _libc is None:
        return False
    return bool(_libc.malloc_trim(0))


def _trim_loop(interval_s: float) -> None:
    while True:
        time.sleep(interval_s)
        try:
            malloc_trim()
        except Exception as e:  # maintenance thread must never die silently
            log.warning("malloc_trim_failed", error=str(e))


def start_periodic_trim(interval_s: float | None = None) -> bool:
    """Spawn a daemon thread calling malloc_trim(0) every ``interval_s``.

    Interval precedence: arg > ``MYCELIUM_MALLOC_TRIM_INTERVAL`` env > 60s.
    A non-positive interval disables trimming. Returns True iff the thread
    was started. No-op off glibc so macOS/musl dev is unaffected.
    """
    if interval_s is None:
        interval_s = float(os.environ.get(_ENV_INTERVAL, _DEFAULT_INTERVAL_S))
    if interval_s <= 0:
        log.info("malloc_trim_disabled")
        return False
    if _libc is None:
        log.info("malloc_trim_unavailable", reason="non-glibc allocator")
        return False
    threading.Thread(
        target=_trim_loop, args=(interval_s,),
        name="mycelium-malloc-trim", daemon=True,
    ).start()
    log.info("malloc_trim_started", interval_s=interval_s)
    return True
