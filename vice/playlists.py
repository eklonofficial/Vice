"""
Playlist store, persisted clip groupings that survive renames and deletes.

Two kinds of playlists:
  • auto:   created when a game is detected at clip-save time; keyed by the
             sanitized game name so filename backfill and live detection
             converge on the same playlist.
  • custom, user-created (name, emoji, gradient colors); clips are added
             from the UI.

Clip identity is the filename stem ("slug"), which is not stable: renames
change it, deletes free clip numbers for reuse. The share server therefore
calls on_clip_renamed / on_clip_deleted from the same handlers that mutate
clip files, keeping membership consistent.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import time
import zlib
from datetime import datetime
from pathlib import Path
from typing import Optional

from importlib.resources import files as _pkg_files

from .runtime import actual_home_dir

log = logging.getLogger("vice.playlists")

PLAYLISTS_PATH = actual_home_dir() / ".local" / "share" / "vice" / "playlists.json"

# Same 8 gradient pairs as PL_COLORS in the UI; auto playlists pick one
# deterministically so a recreated playlist keeps its look.
PL_COLORS = [
    ("#ff7a45", "#9a3412"), ("#f0b429", "#7c4a03"),
    ("#34d399", "#064e3b"), ("#38bdf8", "#075985"),
    ("#8b5cf6", "#3b0a74"), ("#f472b6", "#831843"),
    ("#ef4444", "#7f1d1d"), ("#a3e635", "#3f6212"),
]

_COLOR_RE = re.compile(r"#[0-9a-fA-F]{6}")
# Matches the recorder's clip naming: Vice_Clip_<N>_<Tag>.<ext>, and the same
# shape for screenshots, which carry the IMAGE_PREFIX below.
_TAGGED_CLIP_RE = re.compile(r"^(?:img:)?Vice_(?:Clip|Session|Shot)_\d+_(?P<tag>.+)$")

# Images share this store with clips rather than getting one of their own, so a
# game's auto playlist holds the clips and the screenshots from that game
# together and every membership gesture is the same gesture. Membership has
# always been a list of opaque strings, so the only thing needed to keep the
# two apart is a prefix that cannot occur in a filename stem.
IMAGE_PREFIX = "img:"


def image_slug(stem: str) -> str:
    """The playlist-membership key for the image whose filename stem is *stem*."""
    return f"{IMAGE_PREFIX}{stem}"


def game_key(name: str) -> str:
    """Stable auto-playlist key: the recorder's filename-tag sanitize rule,
    lowercased (recorder._clip_tag)."""
    tag = re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-")[:48]
    return tag.lower()


def build_tag_index(custom_games: list | None = None) -> dict[str, str]:
    """Map sanitized game keys to display names, from the bundled games list
    plus the user's custom games. Used to give backfilled auto playlists
    proper names instead of raw filename tags."""
    index: dict[str, str] = {}
    try:
        raw = json.loads((_pkg_files("vice") / "data" / "games.json").read_text())
        for g in raw:
            name = str(g.get("name", "")).strip()
            if name:
                index[game_key(name)] = name
    except Exception as exc:
        log.debug("Failed loading bundled games list for tag index: %s", exc)
    for g in custom_games or []:
        name = str(getattr(g, "name", "") or "").strip()
        if name:
            index[game_key(name)] = name
    return index


class PlaylistStore:
    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or PLAYLISTS_PATH
        self._playlists: list[dict] = []
        # Game keys whose auto playlist the user deleted. Kept so a deleted auto
        # playlist doesn't get rebuilt from tagged clips on the next restart; a
        # fresh clip of that game revives it.
        self._dismissed_auto: set[str] = set()
        self.load()

    # ── persistence ──────────────────────────────────────────────────────────

    def load(self) -> None:
        self._playlists = []
        self._dismissed_auto = set()
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text())
            items = data.get("playlists", [])
            self._playlists = [p for p in items if isinstance(p, dict) and p.get("id")]
            self._dismissed_auto = {str(k) for k in data.get("dismissed_auto", []) if k}
        except Exception as exc:
            log.warning("Playlists file %s is unreadable: %s", self.path, exc)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({
            "version": 1,
            "playlists": self._playlists,
            "dismissed_auto": sorted(self._dismissed_auto),
        }, indent=2))
        tmp.replace(self.path)

    # ── queries ──────────────────────────────────────────────────────────────

    def list_playlists(self) -> list[dict]:
        return [dict(p, clip_slugs=list(p.get("clip_slugs", []))) for p in self._playlists]

    def get(self, pid: str) -> Optional[dict]:
        for p in self._playlists:
            if p["id"] == pid:
                return p
        return None

    def game_for(self, slug: str) -> Optional[str]:
        for p in self._playlists:
            if p.get("kind") == "auto" and slug in p.get("clip_slugs", []):
                return p.get("game") or p.get("name")
        return None

    # ── custom playlist CRUD ─────────────────────────────────────────────────

    def create_custom(self, name: str, emoji: str = "",
                      color1: str = "", color2: str = "") -> dict:
        name = (name or "").strip()
        if not name:
            raise ValueError("Playlist name is required")
        emoji = (emoji or "").strip()
        if len(emoji) > 4:
            raise ValueError("Playlist emoji must be 4 characters or fewer")
        default1, default2 = PL_COLORS[len(self._playlists) % len(PL_COLORS)]
        color1 = color1 if _COLOR_RE.fullmatch(color1 or "") else default1
        color2 = color2 if _COLOR_RE.fullmatch(color2 or "") else default2
        playlist = {
            "id": f"pl-{int(time.time() * 1000)}-{secrets.token_hex(2)}",
            "kind": "custom",
            "name": name,
            "emoji": emoji,
            "color1": color1,
            "color2": color2,
            "clip_slugs": [],
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        self._playlists.append(playlist)
        self.save()
        return dict(playlist)

    def update_playlist(self, pid: str, fields: dict) -> dict:
        """Edit a playlist's name/emoji/colours. Works for auto playlists too;
        an edited auto playlist keeps auto-filing new clips of its game but is
        marked so it isn't pruned when empty and its look isn't overwritten."""
        p = self.get(pid)
        if not p:
            raise KeyError(pid)
        if "name" in fields:
            name = str(fields["name"] or "").strip()
            if not name:
                raise ValueError("Playlist name is required")
            p["name"] = name
        if "emoji" in fields:
            emoji = str(fields["emoji"] or "").strip()
            if len(emoji) > 4:
                raise ValueError("Playlist emoji must be 4 characters or fewer")
            p["emoji"] = emoji
        for key in ("color1", "color2"):
            if key in fields and _COLOR_RE.fullmatch(str(fields[key] or "")):
                p[key] = fields[key]
        if p.get("kind") == "auto":
            p["edited"] = True
        self.save()
        return dict(p)

    def delete(self, pid: str) -> None:
        p = self.get(pid)
        if not p:
            raise KeyError(pid)
        if p.get("kind") == "auto":
            key = pid[len("auto:"):] if pid.startswith("auto:") else game_key(p.get("game", ""))
            if key:
                self._dismissed_auto.add(key)
        self._playlists.remove(p)
        self.save()

    # ── membership ───────────────────────────────────────────────────────────

    def add_clip(self, pid: str, slug: str) -> dict:
        p = self.get(pid)
        if not p:
            raise KeyError(pid)
        slugs = p.setdefault("clip_slugs", [])
        if slug not in slugs:
            slugs.append(slug)
            self.save()
        return dict(p)

    def remove_clip(self, pid: str, slug: str) -> None:
        p = self.get(pid)
        if not p:
            raise KeyError(pid)
        slugs = p.get("clip_slugs", [])
        if slug in slugs:
            slugs.remove(slug)
            if self._prunable_when_empty(p):
                self._playlists.remove(p)
            self.save()

    @staticmethod
    def _prunable_when_empty(p: dict) -> bool:
        """An empty auto playlist is dropped to keep the rail tidy, unless the
        user has edited it (then it's theirs to keep)."""
        return p.get("kind") == "auto" and not p.get("clip_slugs") and not p.get("edited")

    def set_clip_tag(self, slug: str, game: Optional[str],
                     display_name: Optional[str] = None) -> bool:
        """Manually set (or clear) the game/tag a clip is filed under. Pulls
        the clip out of whatever auto playlist it currently sits in, then
        (when *game* is given) files it under that tag's auto playlist,
        creating it if needed, same mechanism as live detection, just
        triggered by the user instead of a matched process. Passing
        game=None just untags the clip."""
        changed = False
        for p in list(self._playlists):
            if p.get("kind") == "auto" and slug in p.get("clip_slugs", []):
                p["clip_slugs"].remove(slug)
                if self._prunable_when_empty(p):
                    self._playlists.remove(p)
                changed = True
        game = (game or "").strip()
        if game and self.record_auto(game, slug, display_name=display_name or game):
            changed = True
        elif changed:
            self.save()
        return changed

    def record_auto(self, game: str, slug: str,
                    display_name: Optional[str] = None,
                    from_backfill: bool = False) -> bool:
        """Add a clip to its game's auto playlist, creating it if needed.
        Returns True when anything changed."""
        key = game_key(game)
        if not key:
            return False
        if key in self._dismissed_auto:
            # A deleted auto playlist stays gone across restarts (backfill), but
            # a fresh clip of that game revives it.
            if from_backfill:
                return False
            self._dismissed_auto.discard(key)
        pid = f"auto:{key}"
        p = self.get(pid)
        if p is None:
            color1, color2 = PL_COLORS[zlib.crc32(key.encode()) % len(PL_COLORS)]
            p = {
                "id": pid,
                "kind": "auto",
                "name": display_name or game,
                "game": display_name or game,
                "emoji": "",
                "color1": color1,
                "color2": color2,
                "clip_slugs": [],
                "created_at": datetime.now().isoformat(timespec="seconds"),
            }
            self._playlists.append(p)
        slugs = p.setdefault("clip_slugs", [])
        if slug in slugs:
            return False
        slugs.append(slug)
        self.save()
        return True

    # ── clip lifecycle hooks (called by the share server) ────────────────────

    def on_clip_renamed(self, old_slug: str, new_slug: str) -> bool:
        changed = False
        for p in self._playlists:
            slugs = p.get("clip_slugs", [])
            if old_slug in slugs:
                slugs[slugs.index(old_slug)] = new_slug
                changed = True
        if changed:
            self.save()
        return changed

    def on_clip_deleted(self, slug: str) -> bool:
        changed = False
        for p in list(self._playlists):
            slugs = p.get("clip_slugs", [])
            if slug in slugs:
                slugs.remove(slug)
                if self._prunable_when_empty(p):
                    self._playlists.remove(p)
                changed = True
        if changed:
            self.save()
        return changed

    def backfill(self, slugs: set[str], tag_index: dict[str, str],
                 seed_auto: bool = True) -> bool:
        """Sync membership with the clips found on disk at startup: drop slugs
        whose files vanished while the daemon was down, then (when seed_auto)
        seed auto playlists from filename game tags. The vanished-slug cleanup
        always runs; seed_auto=False just skips creating per-game playlists."""
        changed = False
        for p in list(self._playlists):
            kept = [s for s in p.get("clip_slugs", []) if s in slugs]
            if kept != p.get("clip_slugs", []):
                p["clip_slugs"] = kept
                if self._prunable_when_empty(p):
                    self._playlists.remove(p)
                changed = True
        if not seed_auto:
            if changed:
                self.save()
            return changed
        for slug in sorted(slugs):
            m = _TAGGED_CLIP_RE.match(slug)
            if not m:
                continue
            tag = m.group("tag")
            display = tag_index.get(game_key(tag), tag.replace("-", " "))
            if self.record_auto(tag, slug, display_name=display, from_backfill=True):
                changed = True
        if changed:
            self.save()
        return changed
