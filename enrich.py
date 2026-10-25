#!/usr/bin/env python3
"""Names the app looks up on its own, so the user does not have to.

``learning.py`` enforces the spelling of the words the user typed, but it only
knows the words the user thought of. This module closes that gap without
touching a dictation: after delivery, in a background thread, it collects words
that keep coming back and are not known yet, and asks a large free source
(Wikidata, no key, one request per word) how they are written.

Three rules keep that safe:

* **nothing is applied.** A suggestion is offered in Settings -> Leren and only
  becomes law when the user adds it; the app never rewrites text on the word of
  an outside source;
* **capitals only.** A suggestion is dropped unless the source spells the word
  with exactly the same letters. A source that would change letters is wrong
  about private names - ``FreeBuff`` is in no dictionary - and a wrong entry in
  the list is worse than a missing one;
* **once per word.** Every lookup is remembered in ``spelling.json``, so the
  same word is never asked about twice and the list is bounded.

Only words the model itself wrote with a capital somewhere are ever asked
about: that is how a speech model marks a name it half recognises (``Github``,
``Dicteer``), while ordinary words stay lowercase. Words at the start of a
sentence are ignored because their capital says nothing, and words seen only
once are ignored too.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

log = logging.getLogger("dictate")

WIKIDATA = "https://www.wikidata.org/w/api.php"
USER_AGENT = "Dicteer (https://github.com/DrIamlegion/Dicteer)"
TIMEOUT = 4.0

# A word seen once may be a slip; twice is a habit worth a request.
MIN_COUNT = 2
MIN_LENGTH = 4
MAX_PROPOSALS = 12
MAX_TRIED = 500
MAX_TEXTS = 80

_SENTENCE_END = ".!?…:;"
_WORD = re.compile(r"[^\W\d_][\w'’-]*", re.UNICODE)


def tokens(text: str):
    """Yield ``(word, at_sentence_start)`` for the words in one transcript."""
    text = str(text or "")
    for match in _WORD.finditer(text):
        before = text[: match.start()].rstrip()
        yield match.group(0), (not before or before[-1] in _SENTENCE_END)


def unknown_counts(texts, known) -> dict[str, dict]:
    """Words worth asking about: repeated, mid-sentence, not known yet."""
    counts: dict[str, dict] = {}
    for text in texts:
        for word, sentence_start in tokens(text):
            clean = word.strip(".,;:!?'’")
            key = clean.lower()
            if sentence_start or len(clean) < MIN_LENGTH or key in known:
                continue
            entry = counts.setdefault(key, {"forms": {}, "count": 0, "capital": False})
            entry["forms"][clean] = entry["forms"].get(clean, 0) + 1
            entry["count"] += 1
            # The model writes a word with a capital when it hears a name, even
            # when it gets the casing wrong ("Github", "Dicteer"). Only words it
            # treated as a name somewhere are worth asking about: plain lowercase
            # words would come back as "Kunnen" or "Niet" - surnames in Wikidata.
            entry["capital"] = entry["capital"] or any(ch.isupper() for ch in clean)
    return counts


def _picks(counts: dict[str, dict], limit: int) -> list[dict]:
    """The most-used candidates, with every form the model wrote for them."""
    rows = [
        {"key": key, "forms": entry["forms"], "count": entry["count"]}
        for key, entry in counts.items()
        if entry["count"] >= MIN_COUNT and entry["capital"]
    ]
    rows.sort(key=lambda row: (-row["count"], row["key"]))
    return rows[:limit]


def _fix_for(forms: dict[str, int], label: str) -> tuple[str, str] | None:
    """Which written form to correct, and to what, or ``None``.

    Two conditions: the source must spell the same letters (so ``FreeBuff`` is
    never a suggestion from a source that knows nothing about it) and at least
    one written form must actually differ - a name the model already writes
    right every time is not worth a proposal.
    """
    if not label or not forms:
        return None
    if label.lower() != next(iter(forms)).lower():
        return None
    wrong = {form: count for form, count in forms.items() if form != label}
    if not wrong:
        return None
    heard = max(wrong.items(), key=lambda item: item[1])[0]
    return heard, label


def wikidata_lookup(word: str, language: str = "nl", timeout: float = TIMEOUT) -> str | None:
    """The label Wikidata has for this word, when it is the same word.

    ``wbsearchentities`` is a fuzzy search: asking for ``dicteer`` also returns
    ``Dicteerderij``. Only an exact, case-insensitive match counts, and when
    several spellings match, the one with a capital wins - that is the name.
    """
    params = {
        "action": "wbsearchentities",
        "search": word,
        "language": language or "nl",
        "uselang": language or "nl",
        "type": "item",
        "limit": 10,
        "format": "json",
    }
    url = f"{WIKIDATA}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8", "replace"))
    labels = [str(item.get("label") or "") for item in payload.get("search") or []]
    exact = [label for label in labels if _capitals_only(word, label)]
    if not exact:
        return None
    exact.sort(key=lambda label: (not any(ch.isupper() for ch in label), label))
    return exact[0]


class SpellingStore:
    """Suggestions, and the memory of what was already asked about."""

    def __init__(self, path: Path, lookup=None, on_error=None):
        self.path = Path(path)
        self.lookup = lookup or (lambda word, language: wikidata_lookup(word, language))
        self.on_error = on_error
        self.read_error: str | None = None
        self._lock = threading.Lock()
        self._tried: dict[str, dict] = {}
        self._proposals: dict[str, dict] = {}
        self._read()

    # -- disk -------------------------------------------------------------
    def _read(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except Exception as exc:
            log.error("could not read %s: %s", self.path, exc)
            self.read_error = "De naamvoorstellen konden niet worden gelezen."
            return
        if not isinstance(data, dict):
            self.read_error = "De naamvoorstellen konden niet worden gelezen."
            return
        tried = data.get("tried") or {}
        if isinstance(tried, dict):
            for key, entry in tried.items():
                if isinstance(entry, dict):
                    self._tried[str(key)] = {
                        "label": str(entry.get("label") or ""),
                        "ts": float(entry.get("ts") or 0.0),
                    }
        proposals = data.get("proposals") or {}
        if isinstance(proposals, dict):
            for key, entry in proposals.items():
                if not isinstance(entry, dict) or not entry.get("word"):
                    continue
                self._proposals[str(key)] = {
                    "heard": str(entry.get("heard") or key),
                    "word": str(entry["word"]),
                    "count": max(1, int(entry.get("count") or 1)),
                    "ts": float(entry.get("ts") or 0.0),
                }

    def _write_locked(self) -> bool:
        payload = {"version": 1, "tried": self._tried, "proposals": self._proposals}
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            tmp.write_text(json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)
            return True
        except Exception as exc:
            log.error("could not save %s: %s", self.path, exc)
            destination = self.path
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
            callback = self.on_error
            if callback is not None:
                try:
                    callback(f"De naamvoorstellen worden niet opgeslagen in {destination}.")
                except Exception:
                    pass
            return False

    # -- reads ------------------------------------------------------------
    def proposals(self) -> list[dict]:
        with self._lock:
            rows = [dict(entry) for entry in self._proposals.values()]
        return sorted(rows, key=lambda row: (-row["count"], row["heard"].lower()))

    def payload(self) -> dict:
        return {"proposals": self.proposals(), "total": len(self.proposals())}

    def looked_up(self) -> int:
        with self._lock:
            return len(self._tried)

    # -- the pass ---------------------------------------------------------
    def harvest(self, texts, known, language: str = "nl", limit: int = 8) -> dict:
        """Ask about unknown words; return what was looked up and proposed.

        Runs outside any dictation: the network call happens here, in the
        caller's background thread, and a failure only means no suggestion.
        """
        with self._lock:
            skip = {key.lower() for key in known} | set(self._tried) | set(self._proposals)
        picks = _picks(unknown_counts(texts[:MAX_TEXTS], skip), limit)
        looked = added = 0
        for pick in picks:
            key = pick["key"]
            forms = pick["forms"]
            heard = max(forms.items(), key=lambda item: item[1])[0]
            try:
                label = self.lookup(heard, language)
            except Exception as exc:  # network, DNS, a rate limit - all the same
                log.debug("could not look up %r: %s", heard, exc)
                label = None
            looked += 1
            fix = _fix_for(forms, str(label or ""))
            with self._lock:
                self._tried[key] = {"label": str(label or ""), "ts": time.time()}
                if fix:
                    self._proposals[key] = {
                        "heard": fix[0],
                        "word": fix[1],
                        "count": pick["count"],
                        "ts": time.time(),
                    }
                    added += 1
                self._trim_locked()
            log.info("looked up %r: %s", heard, label or "no match")
        with self._lock:
            if looked:
                self._write_locked()
            proposals = [dict(entry) for entry in self._proposals.values()]
        return {"looked": looked, "added": added, "proposals": proposals}

    def _trim_locked(self) -> None:
        if len(self._proposals) > MAX_PROPOSALS:
            keep = sorted(
                self._proposals.items(),
                key=lambda item: (item[1]["count"], item[1]["ts"]),
                reverse=True,
            )
            self._proposals = dict(keep[:MAX_PROPOSALS])
        if len(self._tried) > MAX_TRIED:
            keep = sorted(self._tried.items(), key=lambda item: item[1]["ts"], reverse=True)
            self._tried = dict(keep[:MAX_TRIED])

    # -- the user decides -------------------------------------------------
    def accept(self, heard: str) -> str | None:
        """Drop a proposal and return the spelling to add, if it was there."""
        key = str(heard or "").strip().lower()
        with self._lock:
            entry = self._proposals.pop(key, None)
            if entry is not None:
                self._write_locked()
        return entry["word"] if entry else None

    def dismiss(self, heard: str) -> bool:
        key = str(heard or "").strip().lower()
        with self._lock:
            entry = self._proposals.pop(key, None)
            if entry is not None:
                self._write_locked()
        return entry is not None

    def clear(self) -> None:
        """Forget the suggestions and what was asked; the next pass starts over."""
        with self._lock:
            had = bool(self._proposals or self._tried)
            self._proposals = {}
            self._tried = {}
            if had:
                self._write_locked()
