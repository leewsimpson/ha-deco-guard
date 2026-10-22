"""Diagnostic trace of router actions, for debugging devices that stay offline after an unblock.

Each event is one JSON line in <config>/deco_guard_trace.jsonl (rotated to .1 past MAX_BYTES),
so it can be read without access to the core log. Events are also logged at INFO.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)

TRACE_FILE = "deco_guard_trace.jsonl"
MAX_BYTES = 2_000_000


class Trace:
    """Appends trace events in the executor; never raises."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._path = hass.config.path(TRACE_FILE)
        self._lock = threading.Lock()

    def record(self, event: str, **fields: Any) -> None:
        line = json.dumps(
            {"at": dt_util.utcnow().isoformat(), "event": event, **fields}, default=str
        )
        _LOGGER.info("trace %s", line)
        self._hass.async_add_executor_job(self._append, line)

    def _append(self, line: str) -> None:
        with self._lock:
            try:
                if os.path.exists(self._path) and os.path.getsize(self._path) > MAX_BYTES:
                    os.replace(self._path, f"{self._path}.1")
                with open(self._path, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError as err:
                _LOGGER.warning("Cannot write %s: %s", self._path, err)
