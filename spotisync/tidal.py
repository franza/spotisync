"""Client for the official TIDAL API (openapi.tidal.com/v2, JSON:API)."""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Iterator
from urllib.parse import quote

from spotisync.auth import OAuthConfig, TokenManager
from spotisync.http import ApiClient, ApiError

log = logging.getLogger(__name__)

API_URL = "https://openapi.tidal.com/v2"
AUTHORIZE_URL = "https://login.tidal.com/authorize"
TOKEN_URL = "https://auth.tidal.com/v1/oauth2/token"
DEFAULT_SCOPES = ("user.read", "playlists.read", "playlists.write", "search.read")
DEFAULT_REDIRECT_URI = "http://localhost:8889/callback"
JSONAPI = "application/vnd.api+json"

LOOKUP_BATCH = 20   # ISRCs / IDs per GET /tracks request
ADD_BATCH = 50      # API maximum per POST .../relationships/items
MOVE_BATCH = 20     # API maximum per PATCH .../relationships/items
SEARCH_CANDIDATES = 10
TRACK_INCLUDES = "artists,albums"

_DURATION_RE = re.compile(
    r"^P(?:(?P<d>\d+)D)?(?:T(?:(?P<h>\d+)H)?(?:(?P<m>\d+)M)?(?:(?P<s>\d+(?:\.\d+)?)S)?)?$"
)


@dataclass(frozen=True)
class TidalTrack:
    id: str
    title: str
    version: str | None
    artists: tuple[str, ...]
    isrc: str | None
    duration_s: float | None
    album: str | None = None
    album_type: str | None = None   # "ALBUM", "EP" or "SINGLE"

    @property
    def full_title(self) -> str:
        return f"{self.title} ({self.version})" if self.version else self.title


@dataclass(frozen=True)
class PlaylistItem:
    """One entry of a TIDAL playlist. `item_id` identifies the entry itself
    (needed to remove it); the same track can appear in several entries."""
    track_id: str
    item_id: str


def parse_iso_duration(value: str | None) -> float | None:
    """Parse an ISO 8601 duration such as 'PT3M20S' into seconds."""
    if not value:
        return None
    m = _DURATION_RE.match(value)
    if not m:
        return None
    parts = {k: float(v) if v else 0.0 for k, v in m.groupdict().items()}
    return parts["d"] * 86400 + parts["h"] * 3600 + parts["m"] * 60 + parts["s"]


def parse_tracks(doc: dict) -> list[TidalTrack]:
    """Build TidalTracks from a JSON:API document, resolving included artists and albums."""
    included = {(r.get("type"), r.get("id")): r for r in doc.get("included") or []}

    def related(res: dict, kind: str) -> list[dict]:
        refs = ((res.get("relationships") or {}).get(kind) or {}).get("data") or []
        found = (included.get((kind, ref.get("id"))) for ref in refs)
        return [(r.get("attributes") or {}) for r in found if r]

    data = doc.get("data") or []
    if isinstance(data, dict):
        data = [data]
    tracks = []
    for res in data:
        if res.get("type") != "tracks":
            continue
        attrs = res.get("attributes") or {}
        artists = [a["name"] for a in related(res, "artists") if a.get("name")]
        albums = related(res, "albums")
        album = albums[0] if albums else {}
        tracks.append(TidalTrack(
            id=str(res["id"]),
            title=attrs.get("title") or "",
            version=attrs.get("version") or None,
            artists=tuple(artists),
            isrc=(attrs.get("isrc") or "").upper() or None,
            duration_s=parse_iso_duration(attrs.get("duration")),
            album=album.get("title") or None,
            album_type=(album.get("albumType") or "").upper() or None,
        ))
    return tracks


def chunks(items: list, size: int) -> Iterator[list]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


def added_at_stamps(count: int, now: datetime | None = None) -> list[str]:
    """`count` ISO 8601 UTC timestamps, 1 ms apart, the last one at `now`."""
    end = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    start = end - timedelta(milliseconds=max(count - 1, 0))
    return [
        (start + timedelta(milliseconds=i)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        for i in range(count)
    ]


def make_token_manager(client_id: str, redirect_uri: str,
                       scopes: Iterable[str] = DEFAULT_SCOPES, **kwargs) -> TokenManager:
    return TokenManager(OAuthConfig(
        service="tidal",
        client_id=client_id,
        authorize_url=AUTHORIZE_URL,
        token_url=TOKEN_URL,
        redirect_uri=redirect_uri,
        scopes=tuple(scopes),
    ), **kwargs)


class TidalClient:
    def __init__(self, tokens: TokenManager, *, country_code: str | None = None,
                 api: ApiClient | None = None):
        self.api = api or ApiClient(
            API_URL, tokens,
            headers={"Accept": JSONAPI, "Content-Type": JSONAPI},
            min_interval=0.15,  # TIDAL throttles bursts aggressively
        )
        self._country_code = country_code
        self._user_id: str | None = None

    # ── helpers ─────────────────────────────────────────────────────

    def _next_url(self, doc: dict) -> str | None:
        nxt = (doc.get("links") or {}).get("next")
        if not nxt:
            return None
        if nxt.startswith(("http://", "https://")):
            return nxt
        if nxt.startswith("/v2/"):
            return "https://openapi.tidal.com" + nxt
        return API_URL + "/" + nxt.lstrip("/")

    def _paginate(self, path: str, params: dict) -> Iterator[dict]:
        doc = self.api.get(path, params=params)
        while doc:
            yield doc
            nxt = self._next_url(doc)
            doc = self.api.get(nxt) if nxt else None

    def _load_user(self) -> None:
        doc = self.api.get("users/me")
        data = doc["data"]
        self._user_id = data["id"]
        if not self._country_code:
            self._country_code = (data.get("attributes") or {}).get("country") or "US"

    @property
    def country_code(self) -> str:
        if not self._country_code:
            self._load_user()
        return self._country_code  # type: ignore[return-value]

    @property
    def user_id(self) -> str:
        if not self._user_id:
            self._load_user()
        return self._user_id  # type: ignore[return-value]

    # ── catalog ─────────────────────────────────────────────────────

    def tracks_by_isrc(self, isrcs: Iterable[str]) -> dict[str, list[TidalTrack]]:
        """Look up ISRCs in batches. Returns {ISRC: [tracks]} for those found.

        Fast but incomplete: with several ISRCs in one request, TIDAL returns
        only ONE track per ISRC, e.g. the single even when the same recording
        is also on an album. Use `all_versions` to see every release.
        """
        found: dict[str, list[TidalTrack]] = {}
        unique = sorted({i.upper() for i in isrcs if i})
        for batch in chunks(unique, LOOKUP_BATCH):
            params = {
                "countryCode": self.country_code,
                "filter[isrc]": batch,  # repeated param (OpenAPI form/explode)
                "include": TRACK_INCLUDES,
            }
            for doc in self._paginate("tracks", params):
                for track in parse_tracks(doc):
                    if track.isrc:
                        found.setdefault(track.isrc, []).append(track)
        return found

    def all_versions(self, isrc: str) -> list[TidalTrack]:
        """Every TIDAL track with this ISRC (album, single, compilation, ...).

        Only a single-ISRC request returns all of them, so this costs one
        request (plus pages) per ISRC.
        """
        return self.tracks_by_isrc([isrc]).get(isrc.upper(), [])

    def tracks_by_id(self, ids: Iterable[str]) -> list[TidalTrack]:
        result: list[TidalTrack] = []
        for batch in chunks(list(dict.fromkeys(ids)), LOOKUP_BATCH):
            doc = self.api.get("tracks", params={
                "countryCode": self.country_code,
                "filter[id]": batch,
                "include": TRACK_INCLUDES,
            })
            result.extend(parse_tracks(doc or {}))
        return result

    def search_tracks(self, query: str, limit: int = SEARCH_CANDIDATES) -> list[TidalTrack]:
        """Full-text search; returns up to `limit` tracks with artist names."""
        query = query[:256]
        params = {"countryCode": self.country_code, "include": "tracks"}
        try:
            doc = self.api.get("searchResults", params={**params, "filter[query]": query})
        except ApiError as exc:
            if exc.status not in (400, 404):
                raise
            # Older API revisions took the query as the resource id.
            doc = self.api.get(f"searchResults/{quote(query, safe='')}", params=params)
        ids = self._search_track_ids(doc or {})[:limit]
        if not ids:
            return []
        by_id = {t.id: t for t in self.tracks_by_id(ids)}
        return [by_id[i] for i in ids if i in by_id]

    @staticmethod
    def _search_track_ids(doc: dict) -> list[str]:
        data = doc.get("data") or []
        results = data if isinstance(data, list) else [data]
        ids: list[str] = []
        for res in results:
            rel = ((res.get("relationships") or {}).get("tracks") or {}).get("data") or []
            ids.extend(str(r["id"]) for r in rel if r.get("type") == "tracks")
        if not ids:  # fall back to whatever tracks were included
            ids = [str(r["id"]) for r in doc.get("included") or [] if r.get("type") == "tracks"]
        return list(dict.fromkeys(ids))

    # ── playlists ───────────────────────────────────────────────────

    def find_playlists(self, name: str) -> list[dict]:
        """Return the current user's playlists whose name equals `name`."""
        params = {
            "countryCode": self.country_code,
            "filter[owners.id]": "me",
            "sort": "-lastModifiedAt",
        }
        matches = []
        for doc in self._paginate("playlists", params):
            for res in doc.get("data") or []:
                if (res.get("attributes") or {}).get("name") == name:
                    matches.append(res)
        return matches

    def playlist_items(self, playlist_id: str) -> list[PlaylistItem]:
        """The playlist's track entries in playlist order."""
        items: list[PlaylistItem] = []
        params = {"countryCode": self.country_code}
        for doc in self._paginate(f"playlists/{playlist_id}/relationships/items", params):
            for r in doc.get("data") or []:
                if r.get("type") == "tracks":
                    items.append(PlaylistItem(str(r["id"]), str((r.get("meta") or {}).get("itemId") or "")))
        return items

    def playlist_track_ids(self, playlist_id: str) -> list[str]:
        return [item.track_id for item in self.playlist_items(playlist_id)]

    def create_playlist(self, name: str, description: str = "",
                        access_type: str = "UNLISTED") -> str:
        attributes = {"name": name[:250], "accessType": access_type}
        if description:
            attributes["description"] = description[:500]
        doc = self.api.post(
            "playlists",
            params={"countryCode": self.country_code},
            json={"data": {"type": "playlists", "attributes": attributes}},
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        return str(doc["data"]["id"])

    def add_tracks(self, playlist_id: str, track_ids: list[str],
                   position_before: str | None = None) -> int:
        """Add tracks in order, skipping any already present. Returns count added.

        With `position_before` (the item ID of an existing entry) the tracks
        are inserted in front of that entry; otherwise they are appended.
        Later batches go in front of the same entry, so they follow the
        earlier ones and the order is kept.
        """
        added = 0
        # Give every track its own, strictly increasing "added" time. Without
        # it a whole batch shares one timestamp, and TIDAL apps sorting by
        # date added show those ties in arbitrary order.
        stamps = added_at_stamps(len(track_ids))
        for start in range(0, len(track_ids), ADD_BATCH):
            batch = track_ids[start:start + ADD_BATCH]
            doc = self.api.post(
                f"playlists/{playlist_id}/relationships/items",
                params={"countryCode": self.country_code},
                json={
                    "data": [
                        {"type": "tracks", "id": tid, "meta": {"addedAt": stamps[start + i]}}
                        for i, tid in enumerate(batch)
                    ],
                    "meta": {"onDuplicates": "SKIP",
                             **({"positionBefore": position_before} if position_before else {})},
                },
                # Same key on every retry, so a retried batch is never added twice.
                headers={"Idempotency-Key": str(uuid.uuid4())},
            )
            data = (doc or {}).get("data")
            added += len(data) if isinstance(data, list) else len(batch)
            log.debug("added batch of %d to %s", len(batch), playlist_id)
        return added

    def move_items(self, playlist_id: str, items: list[PlaylistItem],
                   position_before: str) -> None:
        """Move existing entries, in the given order, in front of another entry."""
        for batch in chunks([i for i in items if i.item_id], MOVE_BATCH):
            self.api.request(
                "PATCH",
                f"playlists/{playlist_id}/relationships/items",
                json={
                    "data": [{"type": "tracks", "id": i.track_id, "meta": {"itemId": i.item_id}}
                             for i in batch],
                    "meta": {"positionBefore": position_before},
                },
                headers={"Idempotency-Key": str(uuid.uuid4())},
            )

    def remove_items(self, playlist_id: str, items: list[PlaylistItem]) -> int:
        """Remove specific playlist entries. Returns how many were removed."""
        removable = [i for i in items if i.item_id]
        for batch in chunks(removable, ADD_BATCH):
            self.api.request(
                "DELETE",
                f"playlists/{playlist_id}/relationships/items",
                json={"data": [
                    {"type": "tracks", "id": i.track_id, "meta": {"itemId": i.item_id}}
                    for i in batch
                ]},
                headers={"Idempotency-Key": str(uuid.uuid4())},
            )
            log.debug("removed batch of %d from %s", len(batch), playlist_id)
        return len(removable)
