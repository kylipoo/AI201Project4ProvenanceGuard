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


def get_log(limit=None, status=None):
    """Return entries most-recent-first, optionally filtered/capped.

    `status` filters to one lifecycle state — the appeal queue is just
    get_log(status="under_review"). `limit` caps the count after filtering.
    """
    ordered = list(reversed(_entries))
    if status:
        ordered = [e for e in ordered if e.get("status") == status]
    return ordered[:limit] if limit else ordered


def find_entry(content_id):
    """Return the live record for `content_id`, or None if absent."""
    for entry in _entries:
        if entry.get("content_id") == content_id:
            return entry
    return None


def _persist_all():
    """Rewrite the whole log file from the in-memory entries.

    append_entry() is the fast path for brand-new records. The appeal workflow
    instead *mutates* an existing record in place (status flip + appeal
    attached), which an append-only file can't express — so we rewrite it.
    """
    with open(_LOG_PATH, "w", encoding="utf-8") as f:
        for entry in _entries:
            f.write(json.dumps(entry) + "\n")


def attach_appeal(content_id, creator_reasoning):
    """Attach a creator's appeal to an existing record and flip it to review.

    Returns the mutated record, or None if no record has that content_id. The
    original attribution/confidence/signals are PRESERVED — only `status`
    changes and an `appeal` object is added. No re-classification happens here;
    a human reviewer takes the record from `under_review`. Persists the log.
    """
    record = find_entry(content_id)
    if record is None:
        return None
    record["appeal"] = {
        "creator_reasoning": creator_reasoning,
        "submitted_at": datetime.now(timezone.utc).isoformat(),
    }
    record["status"] = "under_review"
    _persist_all()
    return record


_load()
