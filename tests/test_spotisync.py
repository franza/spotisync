import base64
import hashlib
import json
import time

import pytest
import requests

from spotisync import matching
from spotisync.auth import OAuthConfig, TokenManager, make_pkce_pair
from spotisync.http import ApiClient, ApiError, parse_retry_after
from spotisync.spotify import Playlist, Track, parse_playlist_id, parse_track
from spotisync.sync import entries_to_remove, plan_insertions, split_records, sync_playlist
from spotisync.state import PlaylistState, Record, StateStore
from spotisync.tidal import (PlaylistItem, TidalClient, TidalTrack, parse_iso_duration,
                             parse_tracks)

ID = "3cEYpjA9oz9GiPac4AsH4n"


def sp_track(name="Hey Jude", artists=("The Beatles",), isrc="GBAYE0601690", ms=431000,
             album=None, album_type=None, spotify_id="sp1", added_at=None):
    return Track(name, tuple(artists), isrc, ms, spotify_id, album, album_type, added_at)


def td_track(id="1", title="Hey Jude", artists=("The Beatles",), isrc=None, dur=431.0, version=None,
             album=None, album_type=None):
    return TidalTrack(id, title, version, tuple(artists), isrc, dur, album, album_type)


# ── spotify parsing ──────────────────────────────────────────────────

@pytest.mark.parametrize("ref", [
    ID,
    f"https://open.spotify.com/playlist/{ID}?si=abc",
    f"https://open.spotify.com/intl-de/playlist/{ID}",
    f"spotify:playlist:{ID}",
    f"spotify:user:bob:playlist:{ID}",
])
def test_parse_playlist_id(ref):
    assert parse_playlist_id(ref) == ID


def test_parse_playlist_id_rejects_garbage():
    with pytest.raises(ValueError):
        parse_playlist_id("https://open.spotify.com/album/" + ID)


def test_parse_track_new_item_shape_and_deprecated_boolean():
    entry = {"is_local": False, "track": True, "item": {
        "type": "track", "id": "x", "name": "Song", "duration_ms": 1000,
        "artists": [{"name": "A"}, {"name": "B"}], "external_ids": {"isrc": "USABC1234567"}}}
    assert parse_track(entry) == Track("Song", ("A", "B"), "USABC1234567", 1000, "x")


def test_parse_track_skips_local_episode_and_empty():
    assert parse_track({"is_local": True, "item": {"type": "track", "name": "x"}}) is None
    assert parse_track({"item": {"type": "episode", "name": "Pod"}}) is None
    assert parse_track({"item": None, "track": None}) is None


# ── tidal parsing ────────────────────────────────────────────────────

def test_parse_iso_duration():
    assert parse_iso_duration("PT3M20S") == 200
    assert parse_iso_duration("PT1H0M1.5S") == 3601.5
    assert parse_iso_duration("bogus") is None
    assert parse_iso_duration(None) is None


def test_parse_tracks_resolves_included_artists():
    doc = {
        "data": [{"id": "9", "type": "tracks",
                  "attributes": {"title": "T", "isrc": "usabc1234567", "duration": "PT2M"},
                  "relationships": {"artists": {"data": [{"id": "a1", "type": "artists"}]}}}],
        "included": [{"id": "a1", "type": "artists", "attributes": {"name": "Artist"}}],
    }
    [t] = parse_tracks(doc)
    assert (t.id, t.artists, t.isrc, t.duration_s) == ("9", ("Artist",), "USABC1234567", 120)


# ── matching ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("Hey Jude - Remastered 2015", "hey jude"),
    ("Let It Be (2009 Remaster)", "let it be"),
    ("Señorita (feat. Someone)", "senorita"),
    ("Rock & Roll", "rock and roll"),
])
def test_normalize_title(raw, expected):
    assert matching.normalize_title(raw) == expected


def test_fuzzy_match_requires_artist():
    sp = sp_track(name="Intro", artists=("The xx",), ms=128000)
    wrong = td_track(title="Intro", artists=("M83",), dur=128.0)
    right = td_track(id="2", title="Intro", artists=("The xx",), dur=127.0)
    assert matching.best_fuzzy_match(sp, [wrong]) is None
    assert matching.best_fuzzy_match(sp, [wrong, right]).track.id == "2"


def test_fuzzy_match_rejects_different_duration():
    sp = sp_track(ms=200000)
    assert matching.best_fuzzy_match(sp, [td_track(dur=260.0)]) is None


def test_fuzzy_match_tolerates_remaster_suffix():
    sp = sp_track(name="Hey Jude - Remastered 2015")
    assert matching.best_fuzzy_match(sp, [td_track()]) is not None


def test_isrc_match_prefers_matching_artist():
    sp = sp_track()
    comp = td_track(id="c", artists=("Various Artists",))
    orig = td_track(id="o")
    assert matching.best_isrc_match(sp, [comp, orig]).id == "o"


# ── http ─────────────────────────────────────────────────────────────

class StaticTokens:
    def __init__(self):
        self.refreshes = 0

    def access_token(self):
        return f"tok{self.refreshes}"

    def force_refresh(self):
        self.refreshes += 1
        return self.access_token()


def response(status, body=None, headers=None):
    r = requests.Response()
    r.status_code = status
    r._content = json.dumps(body).encode() if body is not None else b""
    r.headers.update(headers or {})
    r.url = "https://api.test/x"
    return r


class FakeSession(requests.Session):
    def __init__(self, responses):
        super().__init__()
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        return self.responses.pop(0)


def test_parse_retry_after():
    assert parse_retry_after("3", 9) == 3
    assert parse_retry_after(None, 9) == 9
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT", 9) == 0  # in the past
    assert parse_retry_after("garbage", 9) == 9


def test_client_retries_429_then_succeeds(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    session = FakeSession([response(429, headers={"Retry-After": "1"}), response(200, {"ok": 1})])
    client = ApiClient("https://api.test", StaticTokens(), session=session)
    assert client.get("x") == {"ok": 1}
    assert len(session.calls) == 2


def test_client_refreshes_once_on_401():
    tokens = StaticTokens()
    session = FakeSession([response(401), response(204)])
    client = ApiClient("https://api.test", tokens, session=session)
    assert client.get("x") is None  # empty 2xx body is success, not failure
    assert tokens.refreshes == 1
    assert session.calls[1][2]["headers"]["Authorization"] == "Bearer tok1"


def test_client_raises_with_body_on_error():
    session = FakeSession([response(422, {"errors": [{"detail": "bad name"}]})])
    with pytest.raises(ApiError) as exc:
        ApiClient("https://api.test", StaticTokens(), session=session).post("x")
    assert exc.value.status == 422 and "bad name" in exc.value.body


# ── auth ─────────────────────────────────────────────────────────────

def test_pkce_pair_is_s256():
    verifier, challenge = make_pkce_pair()
    assert 43 <= len(verifier) <= 128
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    assert challenge == expected.decode()


def test_token_cache_is_private_and_scope_checked(tmp_path):
    cfg = OAuthConfig("svc", "cid", "https://a", "https://t", "http://127.0.0.1:1/cb", ("s1",))
    mgr = TokenManager(cfg, interactive=False, cache_dir=tmp_path)
    mgr._store_response({"access_token": "A", "refresh_token": "R", "expires_in": 3600})
    assert (mgr.cache_path.stat().st_mode & 0o777) == 0o600
    assert TokenManager(cfg, cache_dir=tmp_path).access_token() == "A"
    other = OAuthConfig("svc", "cid", "https://a", "https://t", "http://127.0.0.1:1/cb", ("s2",))
    assert TokenManager(other, interactive=False, cache_dir=tmp_path)._load() is None


# ── sync ─────────────────────────────────────────────────────────────

class FakeTidal(TidalClient):
    def __init__(self, isrc_hits, search_hits, existing=None, all_hits=None):
        self._country_code, self._user_id = "US", "u"
        self.isrc_hits, self.search_hits = isrc_hits, search_hits
        self.all_hits = all_hits or {}  # single-ISRC lookups: every release
        self.existing = existing  # (playlist_id, [track ids]) or None
        self.created, self.added, self.expanded, self.removed = [], [], [], []
        self.positions = []

    def tracks_by_isrc(self, isrcs):
        return {i: self.isrc_hits[i] for i in isrcs if i in self.isrc_hits}

    def all_versions(self, isrc):
        self.expanded.append(isrc)
        return self.all_hits.get(isrc, self.isrc_hits.get(isrc, []))

    def tracks_by_id(self, ids):
        known = [t for group in (*self.isrc_hits.values(), *self.all_hits.values(),
                                 *self.search_hits.values()) for t in group]
        by_id = {t.id: t for t in known}
        return [by_id[i] for i in ids if i in by_id]

    def search_tracks(self, query, limit=10):
        return self.search_hits.get(query, [])

    def find_playlists(self, name):
        return [{"id": self.existing[0]}] if self.existing else []

    def playlist_items(self, pid):
        if self.existing is None or pid != self.existing[0]:
            raise ApiError("GET", f"playlists/{pid}", 404, "not found")
        return [PlaylistItem(tid, f"entry-{n}") for n, tid in enumerate(self.existing[1])]

    def remove_items(self, pid, items):
        self.removed.append((pid, [i.track_id for i in items]))
        return len(items)

    def create_playlist(self, name, description="", access_type="UNLISTED"):
        self.created.append(name)
        return "new-pl"

    def add_tracks(self, pid, ids, position_before=None):
        self.added.append((pid, ids))
        self.positions.append(position_before)
        return len(ids)


def make_playlist():
    tracks = [
        sp_track(),                                                         # ISRC hit
        sp_track(name="Intro", artists=("The xx",), isrc=None, ms=128000),  # search hit
        sp_track(name="Obscure", artists=("Nobody",), isrc="XX0000000000"),  # miss
        sp_track(),                                                         # duplicate
    ]
    return Playlist(ID, "Mix", "", tracks, skipped=1)


def fake_tidal(existing=None):
    return FakeTidal(
        isrc_hits={"GBAYE0601690": [td_track(id="100")]},
        search_hits={"The xx intro": [td_track(id="200", title="Intro", artists=("The xx",), dur=128.0)]},
        existing=existing,
    )


def test_sync_creates_playlist_and_adds_in_order():
    tidal = fake_tidal()
    result = sync_playlist(tidal, make_playlist())
    assert tidal.created == ["Mix"]
    assert tidal.added == [("new-pl", ["100", "200"])]
    assert [r.method for r in result.results] == ["isrc", "search", None, "isrc"]
    assert len(result.unmatched) == 1 and result.created


def test_sync_updates_existing_playlist_with_missing_tracks_only():
    tidal = fake_tidal(existing=("old-pl", ["100"]))
    result = sync_playlist(tidal, make_playlist())
    assert tidal.created == []
    assert tidal.added == [("old-pl", ["200"])]
    assert result.already_present == 1 and not result.created


def test_dry_run_writes_nothing():
    tidal = fake_tidal()
    result = sync_playlist(tidal, make_playlist(), dry_run=True)
    assert tidal.created == [] and tidal.added == []
    assert result.added == 2 and result.created


# ── playlist order ───────────────────────────────────────────────────

def test_added_at_stamps_are_strictly_increasing_utc():
    from datetime import datetime, timezone
    from spotisync.tidal import added_at_stamps
    now = datetime(2026, 11, 26, 12, 0, 0, tzinfo=timezone.utc)
    stamps = added_at_stamps(3, now)
    assert stamps == ["2026-11-26T11:59:59.998Z", "2026-11-26T11:59:59.999Z",
                      "2026-11-26T12:00:00.000Z"]
    assert added_at_stamps(0) == []


def test_add_tracks_sends_ordered_added_at_across_batches():
    session = FakeSession([response(200, {"data": []}), response(200, {"data": []})])
    api = ApiClient("https://api.test", StaticTokens(), session=session)
    client = TidalClient(None, country_code="US", api=api)
    ids = [str(i) for i in range(60)]  # two batches: 50 + 10
    client.add_tracks("pl", ids)
    sent = [item for _, _, kw in session.calls for item in kw["json"]["data"]]
    assert [item["id"] for item in sent] == ids
    stamps = [item["meta"]["addedAt"] for item in sent]
    assert stamps == sorted(stamps) and len(set(stamps)) == 60


# ── album versions ───────────────────────────────────────────────────

def test_parse_track_reads_album():
    entry = {"item": {"type": "track", "name": "S", "artists": [{"name": "A"}],
                      "album": {"name": "LP", "album_type": "album"}}}
    t = parse_track(entry)
    assert (t.album, t.album_type) == ("LP", "ALBUM")


def test_parse_tracks_reads_included_album():
    doc = {
        "data": [{"id": "9", "type": "tracks", "attributes": {"title": "T", "isrc": "X"},
                  "relationships": {"albums": {"data": [{"id": "al", "type": "albums"}]}}}],
        "included": [{"id": "al", "type": "albums",
                      "attributes": {"title": "LP", "albumType": "ALBUM"}}],
    }
    [t] = parse_tracks(doc)
    assert (t.album, t.album_type) == ("LP", "ALBUM")


SONG_ISRC = "QZK6G2356644"
SINGLE = td_track(id="single", title="Sea", artists=("Deathbrain",), isrc=SONG_ISRC,
                  album="Sea", album_type="SINGLE")
ALBUM = td_track(id="album", title="Sea", artists=("Deathbrain",), isrc=SONG_ISRC,
                 album="a slice of life", album_type="ALBUM")


def album_song():
    return sp_track(name="Sea", artists=("Deathbrain",), isrc=SONG_ISRC,
                    album="a slice of life", album_type="ALBUM")


def test_isrc_match_prefers_same_album_over_single():
    assert matching.best_isrc_match(album_song(), [SINGLE, ALBUM]).id == "album"
    single_song = sp_track(name="Sea", artists=("Deathbrain",), isrc=SONG_ISRC,
                           album="Sea", album_type="SINGLE")
    assert matching.best_isrc_match(single_song, [ALBUM, SINGLE]).id == "single"


def test_batch_returned_only_the_single_so_all_versions_are_fetched():
    # The real TIDAL behaviour: a multi-ISRC request returns one release per ISRC.
    tidal = FakeTidal(isrc_hits={SONG_ISRC: [SINGLE]}, search_hits={},
                      all_hits={SONG_ISRC: [SINGLE, ALBUM]})
    result = sync_playlist(tidal, Playlist(ID, "Mix", "", [album_song()], 0))
    assert tidal.expanded == [SONG_ISRC]
    assert tidal.added == [("new-pl", ["album"])]
    assert result.results[0].method == "isrc"


def test_no_extra_lookup_when_batch_already_has_the_album():
    tidal = FakeTidal(isrc_hits={SONG_ISRC: [ALBUM]}, search_hits={})
    sync_playlist(tidal, Playlist(ID, "Mix", "", [album_song()], 0))
    assert tidal.expanded == []


def test_rerun_does_not_add_album_version_when_single_already_there():
    tidal = FakeTidal(isrc_hits={SONG_ISRC: [SINGLE]}, search_hits={},
                      all_hits={SONG_ISRC: [SINGLE, ALBUM]}, existing=("old-pl", ["single"]))
    result = sync_playlist(tidal, Playlist(ID, "Mix", "", [album_song()], 0))
    assert tidal.added == [] and result.already_present == 1


# ── removal sync ─────────────────────────────────────────────────────

def rec(spotify_id, tidal_id, isrc=None, added_at=None):
    return Record(spotify_id, isrc, added_at, tidal_id, spotify_id)


def song(n, isrc=None):
    """A Spotify track with its own id, ISRC, add time and a TIDAL match."""
    return sp_track(name=f"Song {n}", artists=("Band",), isrc=isrc or f"ISRC{n:08d}",
                    spotify_id=f"sp{n}", added_at=f"2026-01-{n:02d}T00:00:00Z")


def tid_for(n):
    return td_track(id=f"t{n}", title=f"Song {n}", artists=("Band",), isrc=f"ISRC{n:08d}")


def mirror_tidal(spotify_nums, tidal_ids, extra_hits=()):
    hits = {f"ISRC{n:08d}": [tid_for(n)] for n in set(spotify_nums) | set(extra_hits)}
    return FakeTidal(isrc_hits=hits, search_hits={}, existing=("pl", list(tidal_ids)))


def state_for(nums, tidal_playlist_id="pl"):
    return PlaylistState(ID, tidal_playlist_id,
                         [rec(f"sp{n}", f"t{n}", f"ISRC{n:08d}", f"2026-01-{n:02d}T00:00:00Z")
                          for n in nums])


def store_with(tmp_path, state=None):
    store = StateStore(tmp_path)
    if state:
        store.save(state)
    return store


def test_split_records_keeps_present_and_vanished_entries():
    pl = Playlist(ID, "Mix", "", [song(1)], 1, unavailable=("2026-01-03T00:00:00Z",))
    records = state_for([1, 2, 3]).records
    removed, keep = split_records(records, pl)
    assert [r.spotify_id for r in removed] == ["sp2"]          # really removed on Spotify
    assert [r.spotify_id for r in keep] == ["sp1", "sp3"]      # present / info vanished


def test_split_records_matches_by_isrc_when_spotify_id_changed():
    relinked = sp_track(name="Song 1", isrc="ISRC00000001", spotify_id="new-id")
    removed, _ = split_records(state_for([1]).records, Playlist(ID, "Mix", "", [relinked], 0))
    assert removed == []


def test_entries_to_remove_leaves_hand_added_duplicate_and_still_wanted():
    items = [PlaylistItem("t1", "a"), PlaylistItem("t2", "b"), PlaylistItem("t2", "c")]
    gone = [rec("sp1", "t1"), rec("sp2", "t2")]
    chosen = entries_to_remove(gone, items, wanted={"t1"})
    assert chosen == [PlaylistItem("t2", "c")]   # one copy of t2 only; t1 still wanted


def test_sync_removes_tracks_deleted_on_spotify(tmp_path):
    store = store_with(tmp_path, state_for([1, 2, 3]))
    tidal = mirror_tidal([1, 3], ["t1", "t2", "t3"])
    result = sync_playlist(tidal, Playlist(ID, "Mix", "", [song(1), song(3)], 0), store=store)
    assert tidal.removed == [("pl", ["t2"])]
    assert [r.name for r in result.removed] == ["sp2"]
    assert {r.spotify_id for r in store.load(ID).records} == {"sp1", "sp3"}


def test_sync_never_removes_hand_added_tidal_tracks(tmp_path):
    store = store_with(tmp_path, state_for([1]))
    tidal = mirror_tidal([1], ["t1", "manual"])
    sync_playlist(tidal, Playlist(ID, "Mix", "", [song(1)], 0), store=store)
    assert tidal.removed == []


def test_sync_keeps_track_whose_spotify_info_vanished(tmp_path):
    store = store_with(tmp_path, state_for([1, 2]))
    tidal = mirror_tidal([1], ["t1", "t2"])
    pl = Playlist(ID, "Mix", "", [song(1)], 1, unavailable=("2026-01-02T00:00:00Z",))
    sync_playlist(tidal, pl, store=store)
    assert tidal.removed == []
    # Still recorded, so it is removed later if the entry is really deleted.
    assert {r.spotify_id for r in store.load(ID).records} == {"sp1", "sp2"}
    sync_playlist(tidal, Playlist(ID, "Mix", "", [song(1)], 0), store=store)
    assert tidal.removed == [("pl", ["t2"])]


def test_first_sync_with_state_support_removes_nothing_and_records(tmp_path):
    store = store_with(tmp_path)
    tidal = mirror_tidal([1], ["t1", "t9"])
    result = sync_playlist(tidal, Playlist(ID, "Mix", "", [song(1)], 0), store=store)
    assert tidal.removed == [] and any("First sync" in w for w in result.warnings)
    assert [r.tidal_id for r in store.load(ID).records] == ["t1"]


def test_empty_spotify_response_removes_nothing(tmp_path):
    store = store_with(tmp_path, state_for([1, 2]))
    tidal = mirror_tidal([], ["t1", "t2"])
    result = sync_playlist(tidal, Playlist(ID, "Mix", "", [], 0), store=store)
    assert tidal.removed == [] and any("empty" in w for w in result.warnings)
    assert len(store.load(ID).records) == 2


def test_keep_removed_option_and_dry_run(tmp_path):
    store = store_with(tmp_path, state_for([1, 2]))
    tidal = mirror_tidal([1], ["t1", "t2"])
    pl = Playlist(ID, "Mix", "", [song(1)], 0)
    dry = sync_playlist(tidal, pl, store=store, dry_run=True)
    assert [r.name for r in dry.removed] == ["sp2"] and tidal.removed == []
    assert len(store.load(ID).records) == 2          # dry run saves nothing
    sync_playlist(tidal, pl, store=store, remove=False)
    assert tidal.removed == []
    assert len(store.load(ID).records) == 2          # still ours for later


def test_state_follows_the_recorded_playlist_after_rename(tmp_path):
    store = store_with(tmp_path, state_for([1, 2]))
    tidal = mirror_tidal([1], ["t1", "t2"])
    # Renamed on Spotify: no TIDAL playlist has the new name, the state finds it.
    sync_playlist(tidal, Playlist(ID, "Renamed", "", [song(1)], 0), store=store)
    assert tidal.created == [] and tidal.removed == [("pl", ["t2"])]


def test_album_version_already_there_is_recorded_as_ours(tmp_path):
    store = store_with(tmp_path)
    tidal = FakeTidal(isrc_hits={SONG_ISRC: [SINGLE]}, search_hits={},
                      all_hits={SONG_ISRC: [SINGLE, ALBUM]}, existing=("pl", ["single"]))
    tidal.find_playlists = lambda name: [{"id": "pl"}]
    sync_playlist(tidal, Playlist(ID, "Mix", "", [album_song()], 0), store=store)
    assert [r.tidal_id for r in store.load(ID).records] == ["single"]


def test_state_store_round_trip_and_corrupt_file(tmp_path):
    store = StateStore(tmp_path)
    state = PlaylistState(ID, "pl", [Record("sp1", "X", None, "t1", "Song", ("A", "B"))])
    store.save(state)
    assert store.load(ID) == state
    (tmp_path / f"{ID}.json").write_text("{not json")
    assert store.load(ID) is None


# ── insert position ──────────────────────────────────────────────────

def test_plan_insertions_places_new_tracks_before_their_spotify_successor():
    wanted = ["new0", "a", "new1", "new2", "b", "new3"]
    present = {"a": "entry-a", "b": "entry-b"}
    assert plan_insertions(wanted, ["new0", "new1", "new2", "new3"], present) == [
        ("entry-a", ["new0"]),             # re-added at the top
        ("entry-b", ["new1", "new2"]),     # one request for a run of new tracks
        (None, ["new3"]),                  # nothing after it: append
    ]


def test_plan_insertions_on_a_new_playlist_appends_everything():
    assert plan_insertions(["a", "b"], ["a", "b"], {}) == [(None, ["a", "b"])]


def test_sync_inserts_readded_track_at_its_spotify_position(tmp_path):
    # Emotion Engine case: track 1 deleted, then re-added at the top on Spotify.
    store = store_with(tmp_path, state_for([2, 3]))
    tidal = mirror_tidal([1, 2, 3], ["t2", "t3"])
    sync_playlist(tidal, Playlist(ID, "Mix", "", [song(1), song(2), song(3)], 0), store=store)
    assert tidal.added == [("pl", ["t1"])]
    assert tidal.positions == ["entry-0"]   # in front of t2, the first entry


def test_add_tracks_sends_position_before_only_when_given():
    session = FakeSession([response(200, {"data": []}), response(200, {"data": []})])
    client = TidalClient(None, country_code="US",
                         api=ApiClient("https://api.test", StaticTokens(), session=session))
    client.add_tracks("pl", ["1"], position_before="entry-x")
    client.add_tracks("pl", ["2"])
    metas = [kw["json"]["meta"] for _, _, kw in session.calls]
    assert metas == [{"onDuplicates": "SKIP", "positionBefore": "entry-x"},
                     {"onDuplicates": "SKIP"}]
