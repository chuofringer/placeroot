"""Local persistent preferences (issue #315).

A user states "I bike everywhere, I have a dog" once. That document is
exposed as the attachable `placeroot://preferences` resource and as a
small read/update tool. Tools consult it for omitted defaults (travel
mode, pace, household). An explicit argument always wins.

Nothing leaves the machine: the file lives under the user's config
directory (or PLACEROOT_PREFERENCES_PATH) and is never sent upstream.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

MODES = frozenset({"walk", "cycle", "drive"})

# Built-in fallbacks when the document has no mode. Isochrone is walk;
# every other routing tool is drive — the same defaults those tools had
# before preferences existed.
DEFAULT_MODE_ISOCHRONE = "walk"
DEFAULT_MODE_ROUTE = "drive"

ENV_PATH = "PLACEROOT_PREFERENCES_PATH"

def path() -> Path:
    """Where the document lives. Override with PLACEROOT_PREFERENCES_PATH."""
    override = os.environ.get(ENV_PATH)
    if override:
        return Path(override)
    xdg = os.environ.get("XDG_CONFIG_HOME")
    root = Path(xdg) if xdg else Path.home() / ".config"
    return root / "placeroot" / "preferences.json"


def empty() -> dict[str, Any]:
    return {
        "mode": None,
        "pace": None,
        "household": [],
        "note": None,
    }


def load() -> dict[str, Any]:
    """Read the document. Missing or unreadable file is an empty document."""
    dest = path()
    try:
        raw = dest.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return empty()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return empty()
    if not isinstance(data, dict):
        return empty()
    return _normalize(data)


def save(doc: dict[str, Any]) -> dict[str, Any]:
    """Write `doc` and return the normalized form that was stored."""
    stored = _normalize(doc)
    dest = path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(stored, indent=2) + "\n", encoding="utf-8")
    return stored


def clear() -> dict[str, Any]:
    """Delete the file. Returns the empty document."""
    dest = path()
    try:
        dest.unlink()
    except FileNotFoundError:
        pass
    return empty()


def payload() -> dict[str, Any]:
    """The attachable resource / tool-read body. Shared so they cannot drift."""
    return load()


def update(
    *,
    mode: str | None = None,
    pace: str | None = None,
    household: list[str] | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """Merge the given fields into the document. Omitted fields stay as-is."""
    current = load()
    if mode is not None:
        current["mode"] = mode
    if pace is not None:
        current["pace"] = pace
    if household is not None:
        current["household"] = household
    if note is not None:
        current["note"] = note
    return save(current)


def resolve_mode(explicit: str | None, fallback: str) -> str:
    """Pick a travel mode. Explicit always wins, including an explicit default.

    `explicit` is what the caller passed. None means omitted — then the
    stored preference is used if it is a supported mode, else `fallback`.
    """
    if explicit is not None:
        return explicit
    stored = load().get("mode")
    if stored in MODES:
        return stored
    return fallback


def resolve_pace(explicit: str | None) -> str | None:
    if explicit is not None:
        return explicit
    stored = load().get("pace")
    return stored if isinstance(stored, str) and stored.strip() else None


def resolve_household(explicit: list[str] | None) -> list[str]:
    if explicit is not None:
        return _household(explicit)
    return list(load().get("household") or [])


def _normalize(data: dict[str, Any]) -> dict[str, Any]:
    mode = data.get("mode")
    if mode is not None:
        mode = str(mode).strip().lower() or None
        if mode not in MODES:
            mode = None
    pace = data.get("pace")
    if pace is not None:
        pace = str(pace).strip() or None
    note = data.get("note")
    if note is not None:
        note = str(note).strip() or None
    return {
        "mode": mode,
        "pace": pace,
        "household": _household(data.get("household")),
        "note": note,
    }


def _household(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        item = value.strip()
        return [item] if item else []
    if isinstance(value, list):
        out: list[str] = []
        seen: set[str] = set()
        for item in value:
            text = str(item).strip()
            if text and text not in seen:
                seen.add(text)
                out.append(text)
        return out
    return []
