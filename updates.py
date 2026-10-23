#!/usr/bin/env python3
"""Checks GitHub for a newer release.

Nothing here runs at startup: the Over view calls :func:`check` when the user
presses the button, and that single request is the only network traffic the
feature adds. The app otherwise stays offline apart from the model download and
the optional Groq transcription.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.request

log = logging.getLogger("dictate")

REPO = "DrIamlegion/FreebuffDictate"
API_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases/latest"
TIMEOUT = 6.0
USER_AGENT = "FreebuffDictate-update-check"

_VERSION = re.compile(r"^[vV]?(\d+(?:\.\d+){0,2})(?:[-+].*)?$")


def version_key(text: str) -> tuple[int, int, int]:
    """``v1.7.0-beta.1`` -> ``(1, 7, 0)``; raises ValueError for anything else.

    GitHub tags and ``APP_VERSION`` are written differently (``v1.6``,
    ``1.6.0``, ``1.7.0-beta.1``), so compare numbers rather than strings.
    """
    match = _VERSION.match(str(text or "").strip())
    if not match:
        raise ValueError(f"{text!r} is not a version like 1.6 or v1.7.0")
    parts = [int(part) for part in match.group(1).split(".")]
    while len(parts) < 3:
        parts.append(0)
    return parts[0], parts[1], parts[2]


def is_release_url(url: str) -> bool:
    """Only our own release pages may be opened from the window."""
    return str(url or "").startswith(f"https://github.com/{REPO}/releases/")


def _release_url(candidate, tag: str) -> str:
    if is_release_url(candidate):
        return str(candidate)
    return f"https://github.com/{REPO}/releases/tag/{tag}" if tag else RELEASES_PAGE


def check(current: str, opener=None, url: str = API_URL, timeout: float = TIMEOUT) -> dict:
    """Ask GitHub for the latest release and compare it with ``current``.

    Returns plain data for the window: ``available``, ``latest``, ``url`` and a
    message in Dutch when the request could not be made. Any failure is
    reported as data, because a failed check must not interrupt dictation.
    """
    opener = opener or urllib.request.urlopen
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/vnd.github+json",
        },
    )
    try:
        with opener(request, timeout=timeout) as response:
            payload = json.load(response)
    except Exception as exc:  # offline, rate limit, DNS, TLS, HTML page
        log.info("update check failed: %s", exc)
        return {"ok": False, "error": "Kon de releasedienst niet bereiken. Controleer je internetverbinding."}

    if not isinstance(payload, dict):
        return {"ok": False, "error": "De releasedienst gaf een onleesbaar antwoord."}
    tag = str(payload.get("tag_name") or "").strip()
    try:
        latest = version_key(tag)
        current_key = version_key(current)
    except ValueError:
        log.warning("update check: unexpected version in %r", payload.get("tag_name"))
        return {"ok": False, "error": "De releasedienst gaf een onbekende versie."}

    return {
        "ok": True,
        "current": str(current),
        "latest": tag.lstrip("vV"),
        "name": str(payload.get("name") or tag),
        "available": latest > current_key,
        "url": _release_url(payload.get("html_url"), tag),
        "page": RELEASES_PAGE,
    }
