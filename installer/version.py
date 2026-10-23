#!/usr/bin/env python3
"""Print Dicteer's version, or check the files that repeat it.

`installer/version_info.txt` is the Windows version resource PyInstaller embeds
in the exe. It stores the number in four places and cannot read it from
`desktop.py`, so it can quietly drift and leave "Dicteer.exe" claiming
an old version. `--check` compares those fields with `APP_VERSION`; the build
scripts run it, so a stale resource fails instead of shipping.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
VERSION_INFO = APP_DIR / "installer" / "version_info.txt"


def read_app_version() -> str:
    text = (APP_DIR / "desktop.py").read_text(encoding="utf-8")
    match = re.search(r'APP_VERSION = "([^"]+)"', text)
    if not match:
        raise SystemExit("APP_VERSION not found in desktop.py")
    return match.group(1)


def as_parts(version: str) -> tuple[int, ...]:
    """Turn '1.6', 'v1.6.0.0' or '1.7-beta.1' into four comparable numbers."""
    core = version.strip().lstrip("vV")
    core = re.split(r"[-+]", core, maxsplit=1)[0]
    parts = core.split(".")
    if not 1 <= len(parts) <= 4 or any(not part.isdigit() for part in parts):
        raise ValueError(f"{version!r} is not a version like 1.6 or 1.6.0.0")
    numbers = [int(part) for part in parts]
    return tuple(numbers + [0] * (4 - len(numbers)))


def read_version_info() -> dict[str, tuple[int, ...]]:
    """The version numbers version_info.txt embeds, keyed by field name."""
    text = VERSION_INFO.read_text(encoding="utf-8")
    fields: dict[str, tuple[int, ...]] = {}
    for key in ("filevers", "prodvers"):
        match = re.search(rf"{key}\s*=\s*\(([^)]*)\)", text)
        if not match:
            raise ValueError(f"{key} not found in installer\\version_info.txt")
        numbers = tuple(int(number) for number in re.findall(r"\d+", match.group(1)))
        fields[key] = numbers + (0,) * (4 - len(numbers))
    for key in ("FileVersion", "ProductVersion"):
        match = re.search(rf'StringStruct\("{key}",\s*"([^"]*)"\)', text)
        if not match:
            raise ValueError(f"{key} not found in installer\\version_info.txt")
        fields[key] = as_parts(match.group(1))
    return fields


def check(version: str) -> list[str]:
    expected = as_parts(version)
    fields = read_version_info()
    return [
        f"{key} is {'.'.join(str(number) for number in found)}"
        f" but APP_VERSION is {'.'.join(str(number) for number in expected)}"
        for key, found in fields.items()
        if found != expected
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail when installer\\version_info.txt disagrees with APP_VERSION",
    )
    args = parser.parse_args(argv)

    version = read_app_version()
    if not args.check:
        print(version)
        return 0

    try:
        problems = check(version)
    except (OSError, ValueError) as exc:
        print(f"version check failed: {exc}", file=sys.stderr)
        return 1
    if problems:
        print(
            f"installer\\version_info.txt does not match APP_VERSION {version}:",
            file=sys.stderr,
        )
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        print(
            "Update filevers, prodvers, FileVersion and ProductVersion in "
            "installer\\version_info.txt.",
            file=sys.stderr,
        )
        return 1

    print(f"installer\\version_info.txt matches APP_VERSION {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
