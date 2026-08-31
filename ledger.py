"""The dedupe ledger — the single most important component (plan §6).

A private reply is one-shot and irreversible. If we forget that a comment was answered we
burn a real lead, and there is no undo. So the record is a file, committed back to the repo
on every run (`if: always()` in the workflow), exactly like the scheduler's schedule.json.

Never in-memory-only. Never re-derived by re-reading Instagram — the API does not tell you
whether a private reply was sent.

Every comment we have ever looked at ends in one of four states:
  sent      — DM delivered, message_id recorded.
  already   — Meta said the comment already had a reply (subcode 2534023). Terminal.
  expired   — comment older than the 7-day private-reply window. Terminal, never retried.
  failed    — a real error. NOT terminal: retried next run, because the one allowed reply
              was not consumed. Attempts are counted so a permanently broken row goes quiet.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

LEDGER_PATH = Path(__file__).with_name("ledger.json")

# After this many failed attempts we stop retrying a comment and leave it for a human.
MAX_ATTEMPTS = 5

TERMINAL = {"sent", "already", "expired"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Ledger:
    def __init__(self, path: Path = LEDGER_PATH) -> None:
        self.path = path
        if path.exists():
            self.entries: dict[str, dict] = json.loads(path.read_text(encoding="utf-8"))
        else:
            self.entries = {}

    # ---------- queries ----------

    def is_done(self, comment_id: str) -> bool:
        """True if this comment must never be sent to again."""
        entry = self.entries.get(comment_id)
        if not entry:
            return False
        if entry["status"] in TERMINAL:
            return True
        return entry.get("attempts", 0) >= MAX_ATTEMPTS

    def status_of(self, comment_id: str) -> str | None:
        entry = self.entries.get(comment_id)
        return entry["status"] if entry else None

    # ---------- writes (each one flushes to disk immediately) ----------

    def record(self, comment_id: str, status: str, **fields) -> None:
        entry = self.entries.get(comment_id, {})
        entry.update(fields)
        entry["status"] = status
        entry["updated_utc"] = _now()
        if status == "failed":
            entry["attempts"] = entry.get("attempts", 0) + 1
        self.entries[comment_id] = entry
        self.save()

    def save(self) -> None:
        """Flush after EVERY send. A crash mid-loop must not lose earlier sends."""
        self.path.write_text(
            json.dumps(self.entries, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    # ---------- reporting ----------

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for entry in self.entries.values():
            out[entry["status"]] = out.get(entry["status"], 0) + 1
        return out
