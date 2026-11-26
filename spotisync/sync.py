"""Match a Spotify playlist's tracks on TIDAL and copy them into a playlist."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable

from spotisync import matching
from spotisync.spotify import Playlist, Track
from spotisync.tidal import TidalClient

log = logging.getLogger(__name__)


@dataclass
class TrackResult:
    track: Track
    tidal_id: str | None = None
    method: str | None = None       # "isrc" or "search"
    score: float | None = None

    @property
    def matched(self) -> bool:
        return self.tidal_id is not None


@dataclass
class SyncResult:
    playlist: Playlist
    results: list[TrackResult]
    tidal_playlist_id: str | None = None
    created: bool = False
    added: int = 0
    already_present: int = 0
    dry_run: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def matched(self) -> list[TrackResult]:
        return [r for r in self.results if r.matched]

    @property
    def unmatched(self) -> list[TrackResult]:
        return [r for r in self.results if not r.matched]


Progress = Callable[[int, int, TrackResult], None]


def match_tracks(tidal: TidalClient, tracks: list[Track],
                 progress: Progress | None = None) -> list[TrackResult]:
    """Match by ISRC in batches, then fall back to search for the rest."""
    results = [TrackResult(t) for t in tracks]
    by_isrc = tidal.tracks_by_isrc(t.isrc for t in tracks if t.isrc)
    expanded: set[str] = set()

    for i, res in enumerate(results, 1):
        if res.track.isrc:
            isrc = res.track.isrc.upper()
            candidates = by_isrc.get(isrc, [])
            # A batched lookup returns one release per ISRC, often the single.
            # If that isn't the album the Spotify track is from, fetch all
            # releases of the recording once and choose among them.
            if (res.track.album and isrc not in expanded
                    and not any(matching.same_album(res.track, c) for c in candidates)):
                expanded.add(isrc)
                more = tidal.all_versions(isrc)
                seen = {c.id for c in candidates}
                candidates = candidates + [c for c in more if c.id not in seen]
                by_isrc[isrc] = candidates
            hit = matching.best_isrc_match(res.track, candidates)
            if hit:
                res.tidal_id, res.method = hit.id, "isrc"
                if progress:
                    progress(i, len(results), res)
                continue

        candidates = tidal.search_tracks(matching.search_query(res.track))
        match = matching.best_fuzzy_match(res.track, candidates)
        if match:
            res.tidal_id, res.method, res.score = match.track.id, "search", match.score
        if progress:
            progress(i, len(results), res)
    return results


def sync_playlist(tidal: TidalClient, playlist: Playlist, *, dry_run: bool = False,
                  always_new: bool = False, access_type: str = "UNLISTED",
                  progress: Progress | None = None) -> SyncResult:
    results = match_tracks(tidal, playlist.tracks, progress)
    sync = SyncResult(playlist=playlist, results=results, dry_run=dry_run)

    # Keep Spotify order; a recording listed twice is added once.
    wanted = list(dict.fromkeys(r.tidal_id for r in results if r.tidal_id))

    existing_ids: set[str] = set()
    existing_isrcs: set[str] = set()
    if not always_new:
        existing = tidal.find_playlists(playlist.name)
        if len(existing) > 1:
            sync.warnings.append(
                f"{len(existing)} TIDAL playlists are named {playlist.name!r}; "
                "using the most recently modified one."
            )
        if existing:
            sync.tidal_playlist_id = str(existing[0]["id"])
            existing_ids = set(tidal.playlist_track_ids(sync.tidal_playlist_id))
            existing_isrcs = {t.isrc for t in tidal.tracks_by_id(existing_ids) if t.isrc}

    # A song already in the playlist in another release (e.g. the single
    # instead of the album version) counts as present, so re-running never
    # adds the same recording twice.
    isrc_of = {r.tidal_id: (r.track.isrc or "").upper() for r in results if r.tidal_id}
    to_add = [tid for tid in wanted
              if tid not in existing_ids and isrc_of.get(tid) not in existing_isrcs]
    sync.already_present = len(wanted) - len(to_add)

    if dry_run:
        sync.added = len(to_add)
        sync.created = sync.tidal_playlist_id is None
        return sync

    if sync.tidal_playlist_id is None:
        sync.tidal_playlist_id = tidal.create_playlist(
            playlist.name, playlist.description, access_type
        )
        sync.created = True
        log.info("created TIDAL playlist %s", sync.tidal_playlist_id)

    if to_add:
        sync.added = tidal.add_tracks(sync.tidal_playlist_id, to_add)
    return sync
