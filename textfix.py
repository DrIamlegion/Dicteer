#!/usr/bin/env python3
"""Deterministic clean-ups for a finished transcript.

Whisper hears a version number but does not always keep its components apart:
"Dicteer versie twee punt nul een" comes back as ``versie 2.01``, which reads as
the decimal 2,01 - the spoken dots are gone. No rewrite can recover a digit the
model dropped, so these rules stay narrow:

* they fire only right after a word that announces a version (``versie``,
  ``version``, ``build``, ``release``, ``update``, ``patch`` or a glued
  ``v2.01``);
* they only split a milestone that starts with a zero, the one shape a real
  two-part version almost never has, so ``versie 10.12`` and ``2.05 euro`` are
  left exactly as they are.

Runs on every transcript, after the learned corrections and before delivery.
"""

from __future__ import annotations

import re

VERSION_WORDS = frozenset(
    {
        "versie",
        "versies",
        "version",
        "versions",
        "build",
        "builds",
        "release",
        "releases",
        "update",
        "updates",
        "patch",
        "patches",
    }
)

# Words that may sit between two numbers of the same run: "2.01, 2.02 en 2.03".
_CONTINUE = frozenset({"en", "and", "of", "or"})
_EDGE = ",.;:!?()[]{}\"'"

_NUMBER = re.compile(r"\d+(?:\.\d+)+")
_GLUED = re.compile(r"v\d[\w.]*", re.IGNORECASE)
# "2.01" -> "2.0.1", "2.010" -> "2.0.10". A digit or dot on either side means
# the token is already split ("2.0.1") or longer ("192.168.0.1"); leave it alone.
_SPLIT = re.compile(r"(?<![\d.])(\d+)\.0(\d+)(?![\d.])")


def fix_versions(text: str) -> str:
    """Write a spoken version the way it was said: ``versie 2.01`` -> ``2.0.1``."""
    if not text or "." not in text:
        return text

    out: list[str] = []
    inside = False
    for token in text.split(" "):
        core = token.strip(_EDGE)
        lower = core.casefold()
        if lower in VERSION_WORDS:
            inside = True
        elif _GLUED.fullmatch(lower):
            inside = True
            token = _SPLIT.sub(r"\1.0.\2", token)  # v2.01 carries its own prefix
        elif not core or lower in _CONTINUE:
            pass  # punctuation and "en"/"and" keep the run alive
        elif inside and _NUMBER.fullmatch(core):
            token = _SPLIT.sub(r"\1.0.\2", token)
        else:
            inside = False
        out.append(token)
    return " ".join(out)
