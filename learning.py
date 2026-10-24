#!/usr/bin/env python3
"""What the app learns from your corrections.

No model is fine-tuned and nothing leaves the machine. Two small, inspectable
things are learned when the user edits a dictation in the history:

* **word corrections** - ``inluchtpagina`` -> ``inlogpagina`` - applied to
  future transcripts as whole words, case-insensitively;
* **technical vocabulary** - names, acronyms and camelCase words the user typed
  in the correction - added to Whisper's ``initial_prompt`` so it spells them
  the way the user does.

Only single-word replacements are learned as corrections: a rewrite such as
"web socket" -> "WebSocket" would otherwise teach half a phrase to the wrong
word. Multi-word edits can still contribute vocabulary.

Everything lives in ``learning.json`` next to ``config.json`` and can be
forgotten from the Leren card in Settings.
"""

from __future__ import annotations

import difflib
import json
import logging
import os
import re
import threading
import time
from pathlib import Path

import backup

log = logging.getLogger("dictate")

MAX_CORRECTIONS = 200
MAX_VOCABULARY = 120
MAX_PAIRS_PER_EDIT = 8

_TOKEN = re.compile(r"[^\W_][\w'’.-]*", re.UNICODE)

STOPWORDS = {
    "aan", "af", "al", "als", "bij", "dan", "dat", "de", "der", "deze", "die",
    "dit", "door", "een", "en", "er", "geen", "haar", "had", "heb", "heeft",
    "hem", "het", "hier", "hij", "hoe", "hun", "ik", "in", "is", "je", "jij",
    "kan", "maar", "me", "meer", "met", "mijn", "na", "naar", "niet", "nog",
    "nu", "of", "om", "ons", "ook", "op", "over", "te", "tegen", "toch",
    "tot", "uit", "van", "veel", "voor", "was", "wat", "we", "wel", "werd",
    "wie", "wij", "zal", "ze", "zei", "zelf", "zich", "zij", "zijn", "zo",
    "zou", "the", "and", "are", "but", "for", "not", "that", "this", "was",
    "were", "with", "you", "your", "its", "it's", "from", "have", "has",
}


def _key(word: str) -> str:
    return re.sub(r"^[^\w]+|[^\w]+$", "", str(word or "")).lower()


def tokens(text: str) -> list[str]:
    return _TOKEN.findall(str(text or ""))


def _technical(word: str, sentence_start: bool = False) -> bool:
    """A word worth teaching Whisper to spell (names, acronyms, identifiers).

    A capitalised word in the middle of a sentence is a proper noun and worth
    learning; the same capitalisation at the start of a sentence is just
    grammar, so ``sentence_start`` suppresses it (otherwise every corrected
    sentence would add its first word to the prompt).
    """
    clean = re.sub(r"[.,;:!?]+$", "", str(word or "")).strip()
    if len(clean) < 3 or _key(clean) in STOPWORDS:
        return False
    if clean.isupper() and len(clean) >= 2:
        return True
    if any(ch.isupper() for ch in clean[1:]):
        return True
    if clean[:1].isupper() and clean[1:].islower() and not sentence_start:
        return True
    if any(ch.isdigit() for ch in clean):
        return True
    return any(ch in "._-" for ch in clean)


def _match_case(matched: str, fixed: str) -> str:
    if matched.isupper() and len(matched) > 1:
        return fixed.upper()
    if matched[:1].isupper() and fixed[:1].islower():
        return fixed[:1].upper() + fixed[1:]
    return fixed


def _count(value) -> int:
    """A stored count that survives hand-edited or damaged JSON."""
    try:
        return max(1, int(value))
    except (TypeError, ValueError, OverflowError):
        return 1


def _timestamp(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0


class LearningStore:
    """Thread-safe store of corrections and vocabulary, persisted as JSON."""

    def __init__(self, path: Path, on_error=None):
        self.path = Path(path)
        self.on_error = on_error
        self.read_error: str | None = None
        self._write_warned = False
        self._pending_warning: str | None = None
        self._lock = threading.Lock()
        self._corrections: dict[str, dict] = {}
        self._vocabulary: dict[str, dict] = {}
        self._read()
        # A file that could not be read is rewritten in full by the first
        # correction, so it gets a backup before that happens.
        self._backup_pending = self.read_error is not None

    # -- disk -------------------------------------------------------------
    def _read(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except Exception as exc:
            log.error("could not read %s: %s", self.path, exc)
            self.read_error = (
                "De correcties konden niet worden gelezen. "
                "De app is met een lege leerlijst gestart."
            )
            return
        if not isinstance(data, dict):
            log.error("%s does not contain a learning object", self.path)
            self.read_error = (
                "De correcties konden niet worden gelezen. "
                "De app is met een lege leerlijst gestart."
            )
            return
        corrections = data.get("corrections") or {}
        vocabulary = data.get("vocabulary") or {}
        skipped = 0
        if isinstance(corrections, dict):
            for heard, entry in corrections.items():
                if not isinstance(entry, dict) or not entry.get("fixed"):
                    skipped += 1
                    continue
                heard_text = str(heard).strip()
                if not heard_text:
                    skipped += 1
                    continue
                self._corrections[heard_text] = {
                    "fixed": str(entry["fixed"]),
                    "count": _count(entry.get("count")),
                    "ts": _timestamp(entry.get("ts")),
                }
        elif corrections:
            skipped += 1
        if isinstance(vocabulary, dict):
            for word, entry in vocabulary.items():
                if not isinstance(entry, dict):
                    skipped += 1
                    continue
                word_text = str(word).strip()
                if not word_text:
                    skipped += 1
                    continue
                self._vocabulary[word_text] = {
                    "count": _count(entry.get("count")),
                    "ts": _timestamp(entry.get("ts")),
                }
        elif vocabulary:
            skipped += 1
        if skipped:
            log.warning("skipped %d unreadable learning item(s) in %s", skipped, self.path)
            self.read_error = (
                "Sommige leeritems konden niet worden gelezen "
                "en zijn overgeslagen."
            )

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
        # One warning per failed episode: a disk that stays full must not
        # pop a toast after every single correction.
        if not self._write_warned:
            self._write_warned = True
            self._pending_warning = (
                "Correcties worden niet opgeslagen. "
                "Controleer of de map schrijfbaar is."
            )
        return False

    def _write_locked(self) -> bool:
        if not self._save_backup_locked():
            return self._write_failed_locked("the unreadable file could not be backed up")
        payload = {
            "version": 1,
            "corrections": self._corrections,
            "vocabulary": self._vocabulary,
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            tmp.write_text(json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8")
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

    # -- reads ------------------------------------------------------------
    def corrections(self) -> list[dict]:
        with self._lock:
            rows = [
                {"heard": heard, "fixed": entry["fixed"], "count": entry["count"]}
                for heard, entry in self._corrections.items()
            ]
        return sorted(rows, key=lambda row: (-row["count"], row["heard"]))

    def vocabulary(self) -> list[dict]:
        with self._lock:
            rows = [{"word": word, "count": entry["count"]} for word, entry in self._vocabulary.items()]
        return sorted(rows, key=lambda row: (-row["count"], row["word"]))

    def payload(self) -> dict:
        corrections = self.corrections()
        vocabulary = self.vocabulary()
        return {
            "corrections": corrections,
            "vocabulary": vocabulary,
            "total": len(corrections) + len(vocabulary),
        }

    def prompt_extra(self, limit: int = 220) -> str:
        """Vocabulary for Whisper's initial_prompt, most-used first."""
        words = [row["word"] for row in self.vocabulary()]
        if not words:
            return ""
        out = ""
        for word in words:
            candidate = word if not out else f"{out}, {word}"
            if len(candidate) > limit:
                break
            out = candidate
        return out

    def apply_text(self, text: str) -> str:
        """Apply learned whole-word corrections to a transcript."""
        with self._lock:
            pairs = [(heard, entry["fixed"]) for heard, entry in self._corrections.items()]
        if not pairs or not text:
            return text
        # Longest first, so "websocket" wins over "socket" if both were learned.
        for heard, fixed in sorted(pairs, key=lambda pair: -len(pair[0])):
            pattern = re.compile(rf"(?<!\w){re.escape(heard)}(?!\w)", re.IGNORECASE)
            text = pattern.sub(lambda match: _match_case(match.group(0), fixed), text)
        return text

    # -- writes -----------------------------------------------------------
    def learn_from_diff(self, before: str, after: str) -> dict:
        """Learn from one user edit and report what changed."""
        before = str(before or "")
        after = str(after or "")
        if not after.strip() or before == after:
            return {"corrections": 0, "words": 0}

        old_tokens = tokens(before)
        new_tokens = tokens(after)
        old_keys = [_key(word) for word in old_tokens]
        new_keys = [_key(word) for word in new_tokens]
        matcher = difflib.SequenceMatcher(a=old_keys, b=new_keys, autojunk=False)

        learned_pairs: list[tuple[str, str]] = []
        vocabulary: list[tuple[str, int]] = []
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag in ("replace", "insert"):
                vocabulary.extend((new_tokens[index], index) for index in range(j1, j2))
            elif tag == "equal":
                # A pure capitalisation fix ("codebuff" -> "Codebuff") is a
                # proper-noun signal even though no word changed.
                for offset, (old_word, new_word) in enumerate(zip(old_tokens[i1:i2], new_tokens[j1:j2])):
                    if old_word == new_word:
                        continue
                    if not any(ch.isupper() for ch in new_word):
                        continue
                    if _key(old_word) != _key(new_word):
                        continue
                    vocabulary.append((new_word, j1 + offset))
            if tag != "replace":
                continue
            if (i2 - i1) == (j2 - j1):
                for old_word, new_word in zip(old_tokens[i1:i2], new_tokens[j1:j2]):
                    old_key, new_key = _key(old_word), _key(new_word)
                    if not old_key or old_key == new_key:
                        continue
                    if old_key in STOPWORDS or new_key in STOPWORDS:
                        continue
                    if len(old_key) < 3 or len(new_key) < 2:
                        continue
                    learned_pairs.append((old_key, new_word.strip(".,;:!?")))
                    if len(learned_pairs) >= MAX_PAIRS_PER_EDIT:
                        break

        learned_corrections = 0
        learned_words = 0
        now = time.time()
        with self._lock:
            for heard, fixed in learned_pairs:
                entry = self._corrections.get(heard)
                if entry:
                    entry["fixed"] = fixed
                    entry["count"] += 1
                    entry["ts"] = now
                else:
                    self._corrections[heard] = {"fixed": fixed, "count": 1, "ts": now}
                learned_corrections += 1

            for word, index in vocabulary:
                clean = word.strip(".,;:!?")
                previous = new_tokens[index - 1] if index > 0 else ""
                sentence_start = not previous or previous.endswith((".", "!", "?", "…", ":", ";"))
                if not _technical(clean, sentence_start=sentence_start):
                    continue
                entry = self._vocabulary.get(clean)
                if entry:
                    entry["count"] += 1
                    entry["ts"] = now
                else:
                    self._vocabulary[clean] = {"count": 1, "ts": now}
                learned_words += 1

            if learned_corrections or learned_words:
                self._trim_locked()
                self._write_locked()
        self._report_write_failure()

        if learned_corrections or learned_words:
            log.info(
                "learned from correction: %d word(s), %d vocabulary item(s)",
                learned_corrections,
                learned_words,
            )
        return {"corrections": learned_corrections, "words": learned_words}

    def _trim_locked(self) -> None:
        if len(self._corrections) > MAX_CORRECTIONS:
            keep = sorted(self._corrections.items(), key=lambda item: (item[1]["count"], item[1]["ts"]), reverse=True)
            self._corrections = dict(keep[:MAX_CORRECTIONS])
        if len(self._vocabulary) > MAX_VOCABULARY:
            keep = sorted(self._vocabulary.items(), key=lambda item: (item[1]["count"], item[1]["ts"]), reverse=True)
            self._vocabulary = dict(keep[:MAX_VOCABULARY])

    def add_word(self, word: str) -> bool:
        """Remember one word the user typed themselves.

        Edits in the history are the usual way in, but a word the model keeps
        mishearing is easier to add up front: typing ``features`` here puts it
        in the next ``initial_prompt`` without waiting for a correction.
        """
        clean = str(word or "").strip().strip(".,;:!?")
        if not clean or len(clean) > 40 or any(char.isspace() for char in clean):
            return False
        now = time.time()
        with self._lock:
            entry = self._vocabulary.get(clean)
            if entry:
                entry["count"] += 1
                entry["ts"] = now
            else:
                self._vocabulary[clean] = {"count": 1, "ts": now}
            self._trim_locked()
            self._write_locked()
        self._report_write_failure()
        return True

    def forget_correction(self, heard: str) -> bool:
        with self._lock:
            removed = self._corrections.pop(_key(heard), None)
            if removed is None:
                removed = self._corrections.pop(str(heard), None)
            if removed is not None:
                self._write_locked()
        self._report_write_failure()
        return removed is not None

    def forget_word(self, word: str) -> bool:
        with self._lock:
            removed = self._vocabulary.pop(str(word), None)
            if removed is not None:
                self._write_locked()
        self._report_write_failure()
        return removed is not None

    def clear(self) -> None:
        with self._lock:
            had = bool(self._corrections or self._vocabulary)
            self._corrections = {}
            self._vocabulary = {}
            if had:
                self._write_locked()
        self._report_write_failure()
