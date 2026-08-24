"""Local persistent preferences (issue #315).

A user states "I bike everywhere, I have a dog" once. That document is
exposed as the attachable `placeroot://preferences` resource and as a
small read/update tool. Routing tools consult the stored travel mode
when theirs is omitted. An explicit argument always wins. pace and
household are stored for later features; they do not change answers yet.

#410: `lang` is a stored result-language preference (a 2-3 letter code,
e.g. "de", "fra") that geocode/geocode_detailed, resolve_place, and
place_details consult when their own per-call `lang` is omitted — see
resolve_lang. Unlike pace/household it does change answers, the same way
mode does for routing.

Nothing leaves the machine: the file lives under the user's config
directory (or PLACEROOT_PREFERENCES_PATH) and is never sent upstream.
"""

from __future__ import annotations

import json
import os
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

MODES = frozenset({"walk", "cycle", "drive"})

# Built-in fallbacks when the document has no mode. Isochrone is walk;
# every other routing tool is drive — the same defaults those tools had
# before preferences existed.
DEFAULT_MODE_ISOCHRONE = "walk"
DEFAULT_MODE_ROUTE = "drive"

# #410: a language code stored/passed for the result-language preference.
# 2-3 lowercase letters covers both ISO 639-1 ("de", "en") and 639-2/3
# ("fra", "yue") — Overture's names.common keys are themselves BCP-47-ish
# short codes, not validated against a fixed registry, so this is a shape
# check (reject obvious junk) rather than a membership check against a
# closed list of "supported" languages the way MODES is.
_LANG_PATTERN = re.compile(r"^[a-z]{2,3}$")

ENV_PATH = "PLACEROOT_PREFERENCES_PATH"


def is_valid_lang(value: str) -> bool:
    """Shape check for a #410 language code — 2-3 lowercase letters after
    stripping/lowercasing. Public so server.py's preferences() tool can
    reject junk before it ever reaches update()/_normalize, the same way
    it checks mode against MODES."""
    return bool(_LANG_PATTERN.match(str(value).strip().lower()))

_THREAD_LOCK = threading.Lock()


class PreferencesError(Exception):
    """A structured failure the preferences tool can return as JSON."""

    def __init__(self, error: str, detail: str):
        super().__init__(detail)
        self.error = error
        self.detail = detail

    def as_dict(self) -> dict[str, str]:
        return {"error": self.error, "detail": self.detail}


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
        "lang": None,
    }


def load() -> dict[str, Any]:
    """Read the document. A missing file is empty; a torn file is an error.

    Never treat corrupt JSON as empty: an update that did that would
    silently replace the torn file and drop every field it still held.
    """
    dest = path()
    try:
        raw = dest.read_text(encoding="utf-8")
    except FileNotFoundError:
        return empty()
    except OSError as exc:
        raise PreferencesError("io_error", str(exc)) from exc
    except UnicodeError as exc:
        raise PreferencesError(
            "corrupt", "preferences file is not valid UTF-8"
        ) from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PreferencesError(
            "corrupt", "preferences file is not valid JSON"
        ) from exc
    if not isinstance(data, dict):
        raise PreferencesError("corrupt", "preferences file is not a JSON object")
    return _normalize(data)


def save(doc: dict[str, Any]) -> dict[str, Any]:
    """Write `doc` atomically (temp + os.replace) and return the stored form."""
    stored = _normalize(doc)
    dest = path()
    tmp = dest.with_name(dest.name + ".tmp")
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(stored, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, dest)
    except OSError as exc:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise PreferencesError("io_error", str(exc)) from exc
    return stored


def clear() -> dict[str, Any]:
    """Delete the file. Returns the empty document."""
    with _exclusive():
        dest = path()
        try:
            dest.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise PreferencesError("io_error", str(exc)) from exc
        return empty()


def payload() -> dict[str, Any]:
    """The attachable resource / tool-read body. Shared so they cannot drift."""
    with _exclusive():
        return load()


def update(
    *,
    mode: str | None = None,
    pace: str | None = None,
    household: list[str] | None = None,
    note: str | None = None,
    lang: str | None = None,
) -> dict[str, Any]:
    """Merge the given fields into the document. Omitted fields stay as-is.

    Holds an exclusive lock for the load-merge-save so two concurrent
    updates cannot drop each other's fields.
    """
    with _exclusive():
        current = load()
        if mode is not None:
            current["mode"] = mode
        if pace is not None:
            current["pace"] = pace
        if household is not None:
            current["household"] = household
        if note is not None:
            current["note"] = note
        if lang is not None:
            current["lang"] = lang
        return save(current)


def resolve_mode(explicit: str | None, fallback: str) -> str:
    """Pick a travel mode. Explicit always wins, including an explicit default.

    `explicit` is what the caller passed. None means omitted — then the
    stored preference is used if it is a supported mode, else `fallback`.
    A corrupt or unreadable file falls back rather than failing a route.
    """
    if explicit is not None:
        return explicit
    try:
        stored = load().get("mode")
    except PreferencesError:
        return fallback
    if stored in MODES:
        return stored
    return fallback


def resolve_lang(explicit: str | None) -> str | None:
    """Pick a result-language code (#410). `explicit` is the caller's
    per-call `lang` argument; None means omitted, and then the stored
    preference is used if there is one. Neither given returns None — no
    lang override at all, byte-identical to pre-#410 behavior. A corrupt
    or unreadable preferences file behaves the same as no stored lang.
    """
    if explicit is not None:
        return explicit
    try:
        return load().get("lang")
    except PreferencesError:
        return None


@contextmanager
def _exclusive() -> Iterator[None]:
    """Same-process lock plus a POSIX file lock for two MCP processes."""
    dest = path()
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise PreferencesError("io_error", str(exc)) from exc
    lock_path = dest.with_name(dest.name + ".lock")
    with _THREAD_LOCK:
        with lock_path.open("a+") as fh:
            _lock_file(fh)
            try:
                yield
            finally:
                _unlock_file(fh)


def _lock_file(fh) -> None:
    if os.name == "nt":
        return
    import fcntl

    fcntl.flock(fh.fileno(), fcntl.LOCK_EX)


def _unlock_file(fh) -> None:
    if os.name == "nt":
        return
    import fcntl

    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


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
    lang = data.get("lang")
    if lang is not None:
        lang = str(lang).strip().lower() or None
        if lang is not None and not _LANG_PATTERN.match(lang):
            lang = None
    return {
        "mode": mode,
        "pace": pace,
        "household": _household(data.get("household")),
        "note": note,
        "lang": lang,
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
