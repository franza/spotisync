"""Match a Spotify playlist's tracks on TIDAL and copy them into a playlist."""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable

from spotisync import matching
from spotisync.http import ApiError
from spotisync.spotify import Playlist, Track
from spotisync.state import PlaylistState, Record, StateStore
from spotisync.tidal import PlaylistItem, TidalClient

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
    removed: list[Record] = field(default_factory=list)
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


def split_records(records: list[Record], playlist: Playlist) -> tuple[list[Record], list[Record]]:
    """Split recorded entries into (removed on Spotify, keep).

    A record is removed only when its track is no longer in the Spotify
    playlist at all. Entries whose track details vanished (Spotify still lists
    them, but without info) are matched by their `added_at` and kept, because
    the user never removed them.
    """
    current_ids = {t.spotify_id for t in playlist.tracks if t.spotify_id}
    current_isrcs = {t.isrc.upper() for t in playlist.tracks if t.isrc}
    vanished = set(playlist.unavailable)
    removed, keep = [], []
    for r in records:
        still_there = r.spotify_id in current_ids or (r.isrc or "").upper() in current_isrcs
        protected = r.added_at in vanished or (r.added_at is None and bool(vanished))
        (keep if still_there or protected else removed).append(r)
    return removed, keep


def entries_to_remove(removed: list[Record], items: list[PlaylistItem],
                      wanted: set[str]) -> list[PlaylistItem]:
    """The TIDAL entries that represent removed records.

    One entry per removed record, so a copy of the same track the user added
    by hand survives. A track that another Spotify entry still needs is kept.
    """
    budget = Counter(r.tidal_id for r in removed if r.tidal_id not in wanted)
    chosen = []
    for item in reversed(items):  # newest entries first: those are ours more often
        if budget[item.track_id] > 0:
            budget[item.track_id] -= 1
            chosen.append(item)
    return list(reversed(chosen))


def plan_insertions(wanted: list[str], to_add: list[str],
                    present: dict[str, str | None]) -> list[tuple[str | None, list[str]]]:
    """Where to insert new tracks so they land where they are on Spotify.

    `wanted` is the Spotify order, `present` maps a wanted track to the item
    ID of the TIDAL entry that already represents it (or None). Each new
    track goes in front of the nearest following track that is already in
    the playlist; with none after it, it is appended (anchor None). Returns
    (anchor, tracks) groups in Spotify order. Existing entries never move.
    """
    adding = set(to_add)
    anchor_for: dict[str, str | None] = {}
    next_anchor: str | None = None
    for tid in reversed(wanted):
        if tid in adding:
            anchor_for[tid] = next_anchor
        elif present.get(tid):
            next_anchor = present[tid]
    groups: list[tuple[str | None, list[str]]] = []
    for tid in to_add:
        anchor = anchor_for.get(tid)
        if groups and groups[-1][0] == anchor:
            groups[-1][1].append(tid)
        else:
            groups.append((anchor, [tid]))
    return groups


def sync_playlist(tidal: TidalClient, playlist: Playlist, *, dry_run: bool = False,
                  always_new: bool = False, access_type: str = "UNLISTED",
                  remove: bool = True, store: StateStore | None = None,
                  progress: Progress | None = None) -> SyncResult:
    """Make the TIDAL playlist follow the Spotify playlist.

    Adds tracks that are new on Spotify. With `remove`, also removes tracks
    that Spotisync added earlier and that are gone from Spotify now. Tracks
    added to the TIDAL playlist by hand are never touched.
    """
    results = match_tracks(tidal, playlist.tracks, progress)
    sync = SyncResult(playlist=playlist, results=results, dry_run=dry_run)

    # Keep Spotify order; a recording listed twice is added once.
    wanted = list(dict.fromkeys(r.tidal_id for r in results if r.tidal_id))

    state = store.load(playlist.id) if store and not always_new else None
    items: list[PlaylistItem] = []
    if not always_new:
        if state:
            try:
                items = tidal.playlist_items(state.tidal_playlist_id)
                sync.tidal_playlist_id = state.tidal_playlist_id
            except ApiError as exc:
                if exc.status != 404:
                    raise
                sync.warnings.append("The TIDAL playlist from the last sync is gone; "
                                     "looking for one by name.")
                state = None
        if sync.tidal_playlist_id is None:
            existing = tidal.find_playlists(playlist.name)
            if len(existing) > 1:
                sync.warnings.append(
                    f"{len(existing)} TIDAL playlists are named {playlist.name!r}; "
                    "using the most recently modified one."
                )
            if existing:
                sync.tidal_playlist_id = str(existing[0]["id"])
                items = tidal.playlist_items(sync.tidal_playlist_id)

    existing_ids = {i.track_id for i in items}
    id_for_isrc = {t.isrc: t.id for t in tidal.tracks_by_id(existing_ids) if t.isrc} if items else {}

    # A song already in the playlist in another release (e.g. the single
    # instead of the album version) counts as present, so re-running never
    # adds the same recording twice.
    isrc_of = {r.tidal_id: (r.track.isrc or "").upper() for r in results if r.tidal_id}
    to_add = [tid for tid in wanted
              if tid not in existing_ids and isrc_of.get(tid) not in id_for_isrc]
    sync.already_present = len(wanted) - len(to_add)

    # Removals: only entries recorded as ours, and only when Spotify no
    # longer has the track.
    keep: list[Record] = []
    to_remove: list[PlaylistItem] = []
    if state:
        removed, keep = split_records(state.records, playlist)
        if removed and not playlist.tracks and not playlist.unavailable:
            sync.warnings.append("Spotify returned an empty playlist; not removing anything "
                                 "from TIDAL to be safe.")
            keep, removed = keep + removed, []
        if remove:
            to_remove = entries_to_remove(removed, items, set(wanted))
            removed_ids = Counter(i.track_id for i in to_remove)
            for r in removed:
                if removed_ids[r.tidal_id] > 0:
                    removed_ids[r.tidal_id] -= 1
                    sync.removed.append(r)
        else:
            keep = keep + removed  # still ours; a later sync may remove them
    elif items and remove:
        sync.warnings.append("First sync of this playlist with removal support: nothing "
                             "is removed this time. Later syncs remove tracks you delete "
                             "on Spotify.")

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
        entry_of: dict[str, str] = {}
        for item in items:
            entry_of.setdefault(item.track_id, item.item_id)
        present = {tid: entry_of.get(tid) or entry_of.get(id_for_isrc.get(isrc_of.get(tid), ""))
                   for tid in wanted}
        for anchor, group in plan_insertions(wanted, to_add, present):
            sync.added += tidal.add_tracks(sync.tidal_playlist_id, group, position_before=anchor)
    if to_remove:
        tidal.remove_items(sync.tidal_playlist_id, to_remove)

    if store:
        store.save(PlaylistState(
            spotify_playlist_id=playlist.id,
            tidal_playlist_id=sync.tidal_playlist_id,
            records=_new_records(results, id_for_isrc, keep, sync.removed),
        ))
    return sync


def _new_records(results: list[TrackResult], id_for_isrc: dict[str, str],
                 keep: list[Record], removed: list[Record]) -> list[Record]:
    """Records after this sync: every matched Spotify track, plus kept old
    records that no fresh match replaces (e.g. a vanished track's entry, or a
    track whose match failed this time)."""
    fresh = []
    for r in results:
        if not r.tidal_id:
            continue
        isrc = (r.track.isrc or "").upper() or None
        # If another release of the recording was already there, that one
        # represents the track; nothing new was added for it.
        tidal_id = id_for_isrc.get(isrc, r.tidal_id) if isrc else r.tidal_id
        fresh.append(Record(r.track.spotify_id, isrc, r.track.added_at, tidal_id,
                            r.track.name, r.track.artists))
    covered = {(r.spotify_id, r.added_at) for r in fresh}
    carried = [r for r in keep if (r.spotify_id, r.added_at) not in covered and r not in removed]
    return fresh + carried
