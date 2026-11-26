"""Read playlists from the Spotify Web API (shape as of the Feb 2026 changes)."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Iterator

from spotisync.auth import OAuthConfig, TokenManager
from spotisync.http import ApiClient, ApiError

log = logging.getLogger(__name__)

API_URL = "https://api.spotify.com/v1"
AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
SCOPES = ("playlist-read-private", "playlist-read-collaborative")
DEFAULT_REDIRECT_URI = "http://127.0.0.1:8888/callback"
PAGE_SIZE = 50  # API maximum for /playlists/{id}/items

_ID_RE = re.compile(r"^[A-Za-z0-9]{22}$")
_URL_RE = re.compile(r"open\.spotify\.com/(?:intl-[a-z-]+/)?playlist/([A-Za-z0-9]{22})")
_URI_RE = re.compile(r"^spotify:(?:user:[^:]+:)?playlist:([A-Za-z0-9]{22})$")


class SpotifyAccessError(Exception):
    pass


@dataclass(frozen=True)
class Track:
    name: str
    artists: tuple[str, ...]
    isrc: str | None
    duration_ms: int | None
    spotify_id: str | None
    album: str | None = None
    album_type: str | None = None   # "ALBUM", "SINGLE" or "COMPILATION"

    @property
    def label(self) -> str:
        return f"{self.name} — {', '.join(self.artists) or 'unknown artist'}"


@dataclass(frozen=True)
class Playlist:
    id: str
    name: str
    description: str
    tracks: list[Track]
    skipped: int  # local files, episodes and unavailable entries


def parse_playlist_id(value: str) -> str:
    """Accept a playlist ID, open.spotify.com URL or spotify: URI."""
    value = value.strip()
    if _ID_RE.match(value):
        return value
    for pattern in (_URL_RE, _URI_RE):
        if m := pattern.search(value):
            return m.group(1)
    raise ValueError(f"not a Spotify playlist ID, URL or URI: {value!r}")


def parse_track(entry: dict) -> Track | None:
    """Turn one playlist entry into a Track, or None if it can't be synced."""
    if entry.get("is_local"):
        return None
    # Feb 2026: the object moved to "item"; "track" is deprecated and may be a boolean.
    obj = entry.get("item")
    if not isinstance(obj, dict):
        obj = entry.get("track")
    if not isinstance(obj, dict) or obj.get("type", "track") != "track":
        return None
    if obj.get("is_local") or not obj.get("name"):
        return None
    isrc = (obj.get("external_ids") or {}).get("isrc")
    album = obj.get("album") or {}
    return Track(
        name=obj["name"],
        artists=tuple(a["name"] for a in obj.get("artists") or [] if a.get("name")),
        isrc=isrc or None,
        duration_ms=obj.get("duration_ms"),
        spotify_id=obj.get("id"),
        album=album.get("name") or None,
        album_type=(album.get("album_type") or "").upper() or None,
    )


def make_token_manager(client_id: str, redirect_uri: str, **kwargs) -> TokenManager:
    return TokenManager(OAuthConfig(
        service="spotify",
        client_id=client_id,
        authorize_url=AUTHORIZE_URL,
        token_url=TOKEN_URL,
        redirect_uri=redirect_uri,
        scopes=SCOPES,
    ), **kwargs)


class SpotifyClient:
    def __init__(self, tokens: TokenManager, api: ApiClient | None = None):
        self.api = api or ApiClient(API_URL, tokens)

    def _pages(self, path: str, params: dict) -> Iterator[dict]:
        page = self.api.get(path, params=params)
        while page:
            yield page
            page = self.api.get(page["next"]) if page.get("next") else None

    def get_playlist(self, ref: str) -> Playlist:
        playlist_id = parse_playlist_id(ref)
        try:
            meta = self.api.get(f"playlists/{playlist_id}",
                                params={"fields": "id,name,description"})
            tracks: list[Track] = []
            skipped = 0
            for page in self._pages(f"playlists/{playlist_id}/items",
                                    {"limit": PAGE_SIZE, "additional_types": "track"}):
                for entry in page.get("items") or []:
                    track = parse_track(entry)
                    if track is None:
                        skipped += 1
                    else:
                        tracks.append(track)
        except ApiError as exc:
            if exc.status == 403:
                raise SpotifyAccessError(
                    "Spotify refused access to this playlist's tracks. Since February 2026 "
                    "Spotify apps in Development Mode can only read playlists you own or "
                    "collaborate on, so this playlist can't be synced."
                ) from exc
            if exc.status == 404:
                raise SpotifyAccessError(f"Spotify playlist {playlist_id} was not found.") from exc
            raise
        log.debug("fetched %d tracks (%d skipped) from %s", len(tracks), skipped, playlist_id)
        return Playlist(
            id=playlist_id,
            name=meta["name"],
            description=meta.get("description") or "",
            tracks=tracks,
            skipped=skipped,
        )
