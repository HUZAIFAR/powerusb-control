"""
Append-only activity log: socket switches, mains power comings and goings,
timer firings and server restarts.

Written as JSON Lines so an append is a single small write that cannot
corrupt earlier history, and so the file stays greppable. It is trimmed back
to CAP entries whenever it grows past the high-water mark, which keeps the
file bounded without rewriting it on every event.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

CAP = 1500          # entries kept after a trim
HIGH_WATER = 2000   # trim once the file passes this many lines


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class EventLog:
    """Thread-safe activity log with a bounded on-disk history."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._lines = 0
        if path.exists():
            try:
                with path.open("r", encoding="utf-8") as fh:
                    self._lines = sum(1 for _ in fh)
            except OSError:
                self._lines = 0

    def add(self, kind: str, text: str, **extra: Any) -> Dict[str, Any]:
        """
        Record one event.

        kind is one of: socket, mains, timer, system.
        Never raises -- losing a log line must not break switching a socket.
        """
        entry: Dict[str, Any] = {"t": _now(), "kind": kind, "text": text}
        entry.update({k: v for k, v in extra.items() if v is not None})
        with self._lock:
            try:
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(entry) + "\n")
                self._lines += 1
                if self._lines > HIGH_WATER:
                    self._trim()
            except OSError:
                pass
        return entry

    def _trim(self) -> None:
        """Keep only the newest CAP entries. Caller holds the lock."""
        try:
            lines = self.path.read_text(encoding="utf-8-sig").splitlines()
        except OSError:
            return
        keep = lines[-CAP:]
        tmp = self.path.with_suffix(".jsonl.tmp")
        try:
            tmp.write_text("\n".join(keep) + "\n", encoding="utf-8")
            tmp.replace(self.path)
            self._lines = len(keep)
        except OSError:
            pass

    def recent(self, limit: int = 200) -> List[Dict[str, Any]]:
        """Newest entries first."""
        limit = max(1, min(int(limit or 200), 1000))
        with self._lock:
            try:
                lines = self.path.read_text(encoding="utf-8-sig").splitlines()
            except (OSError, UnicodeDecodeError):
                return []
        out: List[Dict[str, Any]] = []
        for line in reversed(lines):
            if len(out) >= limit:
                break
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue          # skip a torn line rather than fail the view
            if isinstance(item, dict):
                out.append(item)
        return out

    def clear(self) -> None:
        with self._lock:
            try:
                self.path.write_text("", encoding="utf-8")
                self._lines = 0
            except OSError:
                pass
