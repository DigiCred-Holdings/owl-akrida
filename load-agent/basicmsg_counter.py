"""Shared exact-count helpers for the basic-message benchmark."""

import os
import time

from gevent.event import Event
from gevent.lock import BoundedSemaphore

_lock = BoundedSemaphore()
_claimed = 0
_ready = 0
_target = int(os.getenv("TARGET_MESSAGE_COUNT", "10000"))
_ready_target = int(os.getenv("LOCUST_USERS", "1"))
_all_ready = Event()
_done = Event()
_send_started_at = None
_send_finished_at = None


def reset(target=None, ready_target=None):
    global _claimed, _ready, _target, _ready_target, _send_started_at, _send_finished_at
    with _lock:
        _claimed = 0
        _ready = 0
        _send_started_at = None
        _send_finished_at = None
        if target is not None:
            _target = int(target)
        if ready_target is not None:
            _ready_target = int(ready_target)
        _all_ready.clear()
        _done.clear()


def target():
    return _target


def claimed():
    with _lock:
        return _claimed


def claim_attempt():
    """Reserve one measured message attempt. Returns False when the target is met."""
    global _claimed, _send_started_at
    with _lock:
        if _claimed >= _target:
            return False
        if _send_started_at is None:
            _send_started_at = time.time()
        _claimed += 1
        return True


def mark_send_complete():
    """Call after a reserved attempt finishes (success or failure)."""
    global _send_finished_at
    with _lock:
        if _claimed >= _target and _send_finished_at is None:
            _send_finished_at = time.time()
            _done.set()


def mark_ready():
    """Mark one Locust user ready after connection setup; block until all users are ready."""
    global _ready
    with _lock:
        _ready += 1
        if _ready >= _ready_target:
            _all_ready.set()
    _all_ready.wait()


def is_done():
    return _done.is_set()


def steady_state_stats():
    with _lock:
        started = _send_started_at
        finished = _send_finished_at or time.time()
        count = _claimed
    if not started:
        return {"count": count, "seconds": 0.0, "rps": 0.0}
    seconds = max(finished - started, 0.0)
    rps = (count / seconds) if seconds > 0 else 0.0
    return {"count": count, "seconds": seconds, "rps": rps}
