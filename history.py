#!/usr/bin/env python3
"""Dictation history: what was said, when, and how long it took.

Stored next to the app in ``history.json`` (newest first, capped) so the window
can offer recent sentences for copying without re-dictating them.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path

import backup

log = logging.getLogger("dictate")


def _number(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _day(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(_number(ts)))


def stats(items: list[dict], now: float | None = None) -> dict:
    """Counts for the window's dashboard, today and all time."""
    today = _day(time.time() if now is None else now)
    result = {
        "today_count": 0,
        "today_words": 0,
        "today_seconds": 0.0,
        "total_count": len(items),
        "total_words": 0,
        "total_seconds": 0.0,
    }
    for item in items:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "")
        words = len(text.split())
        seconds = max(0.0, _number(item.get("duration")))
        result["total_words"] += words
        result["total_seconds"] += seconds
        if _day(item.get("ts", 0)) == today:
            result["today_count"] += 1
            result["today_words"] += words
            result["today_seconds"] += seconds
    result["today_seconds"] = round(result["today_seconds"], 1)
    result["total_seconds"] = round(result["total_seconds"], 1)
    return result


class HistoryStore:
    def __init__(self, path: Path, limit: int = 200, on_error=None):
        self.path = Path(path)
        self.limit = limit
        self.on_error = on_error
        self.read_error: str | None = None
        self._write_warned = False
        self._pending_warning: str | None = None
        self._lock = threading.Lock()
        self._items = self._read()
        # A file that could not be read is rewritten in full by the first
        # change, so it gets a backup before that happens.
        self._backup_pending = self.read_error is not None

    # -- disk -------------------------------------------------------------
    def _read(self) -> list[dict]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except Exception as exc:
            log.error("could not read %s: %s", self.path, exc)
            self.read_error = (
                "De geschiedenis kon niet worden gelezen. "
                "Er is een leeg overzicht gestart."
            )
            return []
        items = data.get("items") if isinstance(data, dict) else data
        if not isinstance(items, list):
            log.error("%s does not contain a history list", self.path)
            self.read_error = (
                "De geschiedenis kon niet worden gelezen. "
                "Er is een leeg overzicht gestart."
            )
            return []
        clean = []
        skipped = 0
        for item in items:
            if not isinstance(item, dict) or not item.get("text"):
                skipped += 1
                continue
            row = dict(item)
            row["duration"] = max(0.0, _number(item.get("duration")))
            row["ts"] = _number(item.get("ts"))
            clean.append(row)
        if skipped:
            log.warning("skipped %d unreadable history row(s) in %s", skipped, self.path)
            self.read_error = (
                "Sommige dictaten konden niet worden gelezen "
                "en zijn overgeslagen."
            )
        return clean

    def _save_backup_locked(self) -> bool:
        """Keep a copy of a file we could not read before overwriting it."""
        if not self._backup_pending:
            return True
        try:
            backup.copy_aside(self.path)
        except Exception as exc:
            log.error("could not keep a copy of the unreadable %s: %s", self.path, exc)
            return False
        self._backup_pending = False
        return True

    def _write_failed_locked(self, reason) -> bool:
        log.error("could not save %s: %s", self.path, reason)
        # Warn once per failed episode; a persistent problem should not
        # produce a toast for every dictation.
        if not self._write_warned:
            self._write_warned = True
            self._pending_warning = (
                "De geschiedenis wordt niet opgeslagen. "
                "Controleer of de map schrijfbaar is."
            )
        return False

    def _write_locked(self) -> bool:
        if not self._save_backup_locked():
            return self._write_failed_locked("the unreadable file could not be backed up")
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        payload = {"version": 1, "saved": time.time(), "items": self._items}
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)
            self._write_warned = False
            self._pending_warning = None
            return True
        except Exception as exc:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
            return self._write_failed_locked(exc)

    def _report_write_failure(self) -> None:
        """Deliver a pending warning after the store lock has been released."""
        with self._lock:
            message = self._pending_warning
            self._pending_warning = None
        if message is None:
            return
        callback = self.on_error
        if callback is not None:
            try:
                callback(message)
            except Exception as exc:
                log.debug("storage warning callback failed: %s", exc)

    # -- api --------------------------------------------------------------
    def add(self, text: str, *, duration: float = 0.0, model: str = "", language="", mode="") -> dict:
        item = {
            "id": uuid.uuid4().hex[:10],
            "ts": time.time(),
            "text": text,
            "duration": round(float(duration or 0.0), 2),
            "model": model or "",
            "language": language or "",
            "mode": mode or "",
        }
        with self._lock:
            self._items.insert(0, item)
            del self._items[self.limit :]
            self._write_locked()
        self._report_write_failure()
        return item

    def items(self) -> list[dict]:
        with self._lock:
            return [dict(item) for item in self._items]

    def get(self, item_id: str) -> dict | None:
        with self._lock:
            for item in self._items:
                if item.get("id") == item_id:
                    return dict(item)
        return None

    def update(self, item_id: str, text: str) -> dict | None:
        """Replace a dictation's text; returns the old and new wording."""
        new_text = " ".join(str(text or "").split())
        if not new_text:
            return None
        result = None
        with self._lock:
            for item in self._items:
                if item.get("id") != item_id:
                    continue
                old_text = item.get("text", "")
                if old_text == new_text:
                    result = {"old": old_text, "item": dict(item), "changed": False}
                    break
                item["text"] = new_text
                item["edited"] = True
                item["edited_ts"] = time.time()
                self._write_locked()
                result = {"old": old_text, "item": dict(item), "changed": True}
                break
        self._report_write_failure()
        return result

    def delete(self, item_id: str) -> bool:
        with self._lock:
            before = len(self._items)
            self._items = [item for item in self._items if item.get("id") != item_id]
            changed = len(self._items) != before
            if changed:
                self._write_locked()
        self._report_write_failure()
        return changed

    def clear(self) -> None:
        with self._lock:
            self._items = []
            self._write_locked()
        self._report_write_failure()

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)
