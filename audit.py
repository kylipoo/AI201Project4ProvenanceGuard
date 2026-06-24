"""Structured audit log for Provenance Guard.

Every attribution decision (and later, every appeal) is appended here as one
JSON object per line (JSON Lines). This is the canonical record that GET /log
surfaces and that the appeals workflow will mutate. We deliberately avoid
print(): the log must be structured and queryable.

An in-memory list mirrors the file so GET /log is fast and the log persists
across restarts (reloaded from disk on import).
"""

import json
import os
from datetime import datetime, timezone

_LOG_PATH = os.path.join(os.path.dirname(__file__), "audit_log.jsonl")

_entries = []


def _load():
    """Reload existing entries from disk on startup so the log persists."""
    if not os.path.exists(_LOG_PATH):
        return
    with open(_LOG_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                _entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # skip a corrupt line rather than crash the whole log


def append_entry(entry):
    """Append one decision record. Stamps a UTC timestamp if absent. Returns it."""
    entry.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
    _entries.append(entry)
    with open(_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
    return entry


def get_log(limit=None):
    """Return entries most-recent-first, optionally capped to `limit`."""
    ordered = list(reversed(_entries))
    return ordered[:limit] if limit else ordered


_load()
