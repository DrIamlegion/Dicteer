#!/usr/bin/env python3
"""Keeps a copy of a JSON file that could not be read.

The stores rewrite their whole file on every change, so a file that failed to
parse at startup would be gone after the first dictation or correction. Before
that first write the original bytes are copied next to it under a timestamped
name, which keeps the damaged data recoverable by hand.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

log = logging.getLogger("dictate")


def backup_path(path: Path) -> Path:
    """A free ``<name>.<timestamp>.bak`` path next to ``path``."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    candidate = path.with_name(f"{path.name}.{stamp}.bak")
    counter = 2
    while candidate.exists():
        candidate = path.with_name(f"{path.name}.{stamp}-{counter}.bak")
        counter += 1
    return candidate


def copy_aside(path: Path) -> Path | None:
    """Copy ``path`` to a timestamped sibling.

    ``None`` means there was no file to copy. ``OSError`` means the copy could
    not be made, which the caller should treat as "do not overwrite this file".
    """
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return None
    target = backup_path(path)
    target.write_bytes(data)
    log.warning("kept a copy of the unreadable %s as %s", path.name, target.name)
    return target
