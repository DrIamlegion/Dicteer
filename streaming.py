#!/usr/bin/env python3
"""Incremental "words as you speak" transcription.

Hold-to-talk needs text *while* the user is still talking, but re-transcribing
the whole recording on every tick gets slower the longer someone speaks. This
module keeps a committed transcript plus a commit pointer into the audio: each
tick it only transcribes the uncommitted tail (with a little overlap), appends
whatever is genuinely new, and moves the pointer.

The overlap exists so a word split across two ticks is not lost; the cost is
that the tail's first words repeat the committed ending, which
``merge_overlap`` removes by matching word-by-word (ignoring case/punctuation).

Nothing here touches audio devices, the UI or the model directly - the caller
passes a ``transcribe(audio)->str`` callable - so the chunking and de-duplication
logic can be tested with a stub.
"""

from __future__ import annotations

import logging
import re
import threading
import time

import numpy as np

log = logging.getLogger("dictate")

_PUNCT = re.compile(r"[^\w']+", re.UNICODE)


def words(text: str) -> list[str]:
    return [w for w in (text or "").split() if w]


def _norm(word: str) -> str:
    return _PUNCT.sub("", word).lower()


def merge_overlap(committed: str, fresh: str, max_words: int = 8) -> str:
    """Return only the part of ``fresh`` that goes beyond ``committed``.

    The two are compared on their word boundary, so a re-transcribed overlap of
    "...open the config file" followed by "the config file and save" yields
    "and save" rather than duplicating "the config file". When nothing matches,
    the whole of ``fresh`` is treated as new - a duplicate preview word is
    better than silently dropping speech.
    """
    tail = words(fresh)
    if not tail:
        return ""
    head = words(committed)
    if not head:
        return " ".join(tail)
    for n in range(min(max_words, len(head), len(tail)), 0, -1):
        if [_norm(w) for w in head[-n:]] == [_norm(w) for w in tail[:n]]:
            return " ".join(tail[n:])
    return " ".join(tail)


class StreamTranscriber:
    """Turns a growing recording into progressively longer preview text."""

    def __init__(
        self,
        transcribe,
        on_text=None,
        *,
        rate: int,
        chunk_seconds: float = 1.6,
        overlap_seconds: float = 0.8,
        poll_seconds: float = 0.45,
        max_seconds: float = 120.0,
    ):
        self._transcribe = transcribe
        self._on_text = on_text
        self.rate = int(rate)
        self._chunk_samples = max(1, int(chunk_seconds * self.rate))
        self._overlap_samples = max(0, int(overlap_seconds * self.rate))
        self._poll = max(0.1, float(poll_seconds))
        self._max_samples = int(max_seconds * self.rate)

        self._blocks: list[np.ndarray] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        self._committed = ""
        self._committed_at = 0  # sample index of the end of the committed audio
        self._total = 0
        self._updates = 0
        self._previews = 0
        self._muted = False

    # -- api --------------------------------------------------------------
    @property
    def text(self) -> str:
        return self._committed

    @property
    def previews(self) -> int:
        return self._previews

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="stream-preview", daemon=True)
        self._thread.start()

    def push(self, block) -> None:
        """Append a mono float32 block (any length) coming off the mic."""
        if block is None or len(block) == 0:
            return
        with self._lock:
            if self._total >= self._max_samples:
                return
            self._blocks.append(np.asarray(block, dtype=np.float32).copy())
            self._total += len(block)

    def stop(self, timeout: float = 4.0) -> str:
        # Mute first: a transcription that is already running may finish after
        # the caller has moved on, and its preview must not overwrite the final
        # wording on the overlay.
        self._muted = True
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
        return self._committed

    def audio(self) -> np.ndarray:
        with self._lock:
            blocks = list(self._blocks)
        if not blocks:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(blocks)

    # -- internals --------------------------------------------------------
    def _loop(self) -> None:
        while not self._stop.wait(self._poll):
            try:
                self._preview_once()
            except Exception as exc:  # a failed preview must not end the recording
                log.debug("preview tick failed: %s", exc)

    def _preview_once(self) -> None:
        with self._lock:
            blocks = list(self._blocks)
        if not blocks:
            return
        audio = np.concatenate(blocks)
        total = audio.size
        if total <= self._committed_at:
            # Everything so far is already in the transcript. Without this, a
            # configuration where the overlap equals the chunk would re-run the
            # model on the same audio on every poll for as long as the key is
            # held, which is pure heat.
            return
        start = max(0, self._committed_at - self._overlap_samples)
        if total - start < self._chunk_samples:
            return

        started = time.time()
        text = self._transcribe(audio[start:])
        self._previews += 1
        log.debug("preview %.1fs in %.2fs: %r", (total - start) / self.rate, time.time() - started, text)
        if not text:
            return

        addition = merge_overlap(self._committed, text)
        if not addition.strip():
            return
        self._committed = (self._committed + " " + addition).strip()
        self._committed_at = total
        self._updates += 1
        if self._muted:
            return
        if self._on_text is not None:
            try:
                self._on_text(self._committed)
            except Exception as exc:
                log.debug("preview callback failed: %s", exc)
