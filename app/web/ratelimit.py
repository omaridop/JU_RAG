"""Dependency-free, in-memory per-IP rate limiter (sliding window).

Suitable for the single-process app server this project ships with. State is
per-process (resets on restart), which is the right trade-off for abuse
protection — the persistent monthly *budget* cap (token cost) is enforced
separately in the chat layer.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

from ..config import settings

_lock = threading.Lock()
_hits: dict[str, deque] = defaultdict(deque)
_last_gc = 0.0


def _limits() -> list[tuple[int, int]]:
    """(max_requests, window_seconds) pairs; windows with max<=0 are ignored."""
    return [(settings.RATE_LIMIT_PER_MIN, 60),
            (settings.RATE_LIMIT_PER_HOUR, 3600),
            (settings.RATE_LIMIT_PER_DAY, 86400)]


def allow(ip: str) -> bool:
    """Record a hit for ``ip`` and return False if any window is exceeded."""
    global _last_gc
    limits = [(n, w) for n, w in _limits() if n > 0]
    if not limits:
        return True
    now = time.time()
    max_window = max(w for _, w in limits)
    with _lock:
        dq = _hits[ip]
        cutoff = now - max_window
        while dq and dq[0] < cutoff:
            dq.popleft()
        for max_n, window in limits:
            start = now - window
            if sum(1 for ts in dq if ts >= start) >= max_n:
                return False
        dq.append(now)
        # Opportunistic GC so idle IPs don't leak memory.
        if now - _last_gc > 600:
            _last_gc = now
            for k in [k for k, d in _hits.items() if not d or d[-1] < cutoff]:
                _hits.pop(k, None)
        return True


def client_ip() -> str:
    from flask import request
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.remote_addr or "unknown"
