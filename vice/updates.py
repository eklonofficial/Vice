"""
Update check, is there a newer Vice release on GitHub?

Deliberately quiet: at most one request a day, nothing is shown unless the
release is strictly newer than what is installed, and every failure path is
silent. Only fetch_latest touches the network; the rest is pure so the
version comparison and the note summarising are testable without it.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Optional
from urllib.request import Request, urlopen

from . import __version__
from .runtime import actual_home_dir

log = logging.getLogger("vice.updates")

RELEASES_URL = "https://api.github.com/repos/eklonofficial/Vice/releases/latest"
RELEASE_PAGE = "https://github.com/eklonofficial/Vice/releases/latest"
CACHE_PATH = actual_home_dir() / ".local" / "share" / "vice" / "update.json"

# One check a day. The unauthenticated GitHub limit is 60/hour per IP, so
# this is nowhere near it, and the ETag makes the repeat calls cheap.
CHECK_INTERVAL = 24 * 60 * 60
NOTE_LIMIT = 3
NOTE_CHARS = 110

_VERSION_RE = re.compile(r"^\s*v?(\d+(?:\.\d+)*)")
# The release notes lead each item with a bold title: "- **Thing.** detail".
_BOLD_LEAD_RE = re.compile(r"^\s*[-*]\s+\*\*(.+?)\*\*")
_BULLET_RE = re.compile(r"^\s*[-*]\s+(.+)")
_MD_NOISE_RE = re.compile(r"[`*_]|\[(.+?)\]\(.+?\)")
# "(#121)" means nothing out of the context of the full release notes.
_ISSUE_REF_RE = re.compile(r"\s*\(#\d+\)")


def parse_version(text: str) -> Optional[tuple[int, ...]]:
    """Turn "v2.4.0" or "2.4" into a comparable tuple, or None."""
    match = _VERSION_RE.match(str(text or ""))
    if not match:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def is_newer(candidate: str, installed: str) -> bool:
    """Whether *candidate* is a release worth telling the user about.

    Equal, older and unparseable all return False, which is what keeps a
    development build (ahead of the newest tag) quiet.
    """
    new, have = parse_version(candidate), parse_version(installed)
    if new is None or have is None:
        return False
    return new > have


def summarize_notes(body: str, limit: int = NOTE_LIMIT) -> list[str]:
    """A few short lines from a release body, for the notice card."""
    leads: list[str] = []
    bullets: list[str] = []
    for raw in (body or "").splitlines():
        lead = _BOLD_LEAD_RE.match(raw)
        if lead:
            leads.append(lead.group(1))
            continue
        bullet = _BULLET_RE.match(raw)
        if bullet:
            bullets.append(bullet.group(1))
    lines = leads or bullets
    out = []
    for line in lines[:limit]:
        clean = _ISSUE_REF_RE.sub("", _MD_NOISE_RE.sub(r"\1", line)).strip().rstrip(".")
        if not clean:
            continue
        if len(clean) > NOTE_CHARS:
            clean = clean[:NOTE_CHARS].rsplit(" ", 1)[0] + "…"
        out.append(clean)
    return out


def fetch_latest(etag: Optional[str] = None, timeout: float = 8.0) -> Optional[dict]:
    """Latest published release, or None.

    None covers every uninteresting outcome: unchanged since the cached
    etag, a draft or prerelease, no network, a rate limit, malformed JSON.
    The caller never has to handle an exception.
    """
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": f"vice/{__version__}",
    }
    if etag:
        headers["If-None-Match"] = etag
    try:
        with urlopen(Request(RELEASES_URL, headers=headers), timeout=timeout) as resp:
            if getattr(resp, "status", 200) == 304:
                return None
            data = json.loads(resp.read().decode("utf-8"))
            new_etag = resp.headers.get("ETag")
    except Exception as exc:
        log.debug("Update check failed: %s", exc)
        return None

    if not isinstance(data, dict) or data.get("draft") or data.get("prerelease"):
        return None
    version = str(data.get("tag_name") or "").lstrip("vV")
    if not parse_version(version):
        return None
    return {
        "version": version,
        "url": data.get("html_url") or RELEASE_PAGE,
        "notes": summarize_notes(data.get("body") or ""),
        "etag": new_etag,
    }


class UpdateCache:
    """Last known release plus when it was fetched, so a restart does not
    mean another request."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or CACHE_PATH

    def load(self) -> dict:
        try:
            data = json.loads(self.path.read_text())
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def save(self, data: dict) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data))
            tmp.replace(self.path)
        except OSError as exc:
            log.debug("Could not write the update cache: %s", exc)

    def stale(self, now: Optional[float] = None,
              interval: float = CHECK_INTERVAL) -> bool:
        checked = self.load().get("checked_at")
        if not isinstance(checked, (int, float)):
            return True
        return (now if now is not None else time.time()) - checked >= interval


def available(cache: dict, installed: str = __version__) -> Optional[dict]:
    """The payload the UI should be given, or None when there is nothing to
    report. Shape: {version, url, notes}."""
    version = str(cache.get("version") or "")
    if not is_newer(version, installed):
        return None
    return {
        "version": version,
        "url": cache.get("url") or RELEASE_PAGE,
        "notes": list(cache.get("notes") or []),
    }
