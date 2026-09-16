"""Analytics event tracking."""

import time

_EVENT_LOG = []


def track_event(name, properties=None):
    """Record an analytics event with a timestamp."""
    event = {
        "name": name,
        "properties": properties or {},
        "ts": time.time(),
    }
    _EVENT_LOG.append(event)
    return event


def recent_events(limit=50):
    """Return the most recent events, newest first."""
    return list(reversed(_EVENT_LOG[-limit:]))
