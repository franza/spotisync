# AGENTS.md

Notes for AI coding agents working on Spotisync. Read the README first for
what the tool does and how users set it up.

## Setup and checks

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
pytest                 # must pass before every commit
python -m pyflakes spotisync tests   # optional: pip install pyflakes
```

Python 3.10+. The runtime dependencies are only `requests` and `python-dotenv`;
don't add an SDK (spotipy, tidalapi, …) without a strong reason.

## Layout

| Module | Responsibility |
|---|---|
| `spotisync/auth.py` | OAuth PKCE login, local callback server, token cache (`~/.config/spotisync/`, mode 0600) |
| `spotisync/http.py` | `ApiClient`: JSON requests, retries on 429/5xx with `Retry-After`, one token refresh on 401, raises `ApiError` |
| `spotisync/spotify.py` | Read a Spotify playlist into `Track`s |
| `spotisync/tidal.py` | TIDAL catalog lookups, search, playlist reads and writes; `TidalTrack` |
| `spotisync/matching.py` | Pure functions: normalisation, scoring, choosing a TIDAL release |
| `spotisync/state.py` | Per-playlist record of which TIDAL tracks Spotisync added (`~/.config/spotisync/state/`) |
| `spotisync/sync.py` | Orchestration: match all tracks, find or create the playlist, add missing tracks, remove ones deleted on Spotify |
| `spotisync/cli.py` | argparse CLI and `Settings.from_env()` (env vars and `.env`) |

Keep this split: API clients don't decide matches, `matching.py` makes no
HTTP calls, and user-facing output lives in `cli.py` (the only exception is
the interactive login prompts in `auth.py`).

## Behaviour that must not regress

- **Match before writing.** Create the TIDAL playlist only after matching,
  so a failure never leaves an empty playlist.
- **No duplicates.** A track counts as already present if its TIDAL ID *or
  its ISRC* is in the playlist. Spotisync never reorders existing items.
- **Only remove what Spotisync added.** `state.py` records, per Spotify
  playlist, which TIDAL tracks Spotisync put there. Removal candidates come
  only from those records (`split_records`, `entries_to_remove`); tracks the
  user added on TIDAL by hand must never be removed. Specifically:
  - a record is removed only when neither its Spotify ID nor its ISRC is in
    the Spotify playlist any more;
  - entries Spotify still lists without track details (`Playlist.unavailable`)
    protect records with the same `added_at`;
  - an empty Spotify response removes nothing;
  - no state means no removals (the first sync only records);
  - a dry run writes nothing, not even state.
- **Order follows Spotify.** Every added item gets its own increasing
  `meta.addedAt` (`added_at_stamps`). Without that, a whole batch shares one
  timestamp and TIDAL apps sorting by date added show it in arbitrary order.
- **Same album as Spotify.** When several TIDAL releases share an ISRC,
  prefer the one from the Spotify track's album (`best_isrc_match`).
- **Search never guesses.** A fallback search result needs a similar title,
  the same primary artist and a duration within 7 s; otherwise the track is
  reported as missing.

## API facts learned the hard way

Spotify (Web API, rules in force since February 2026):
- Playlist tracks come from `GET /playlists/{id}/items` (max `limit=50`;
  follow `next`). The track object is under `item`; the old `track` key is
  deprecated and may be a boolean.
- Only playlists the user owns or collaborates on are readable, and only with
  a user token; Client Credentials doesn't work. A 403 means "not your
  playlist", not a bug.
- `external_ids.isrc`, `duration_ms`, `artists` and `album` are available;
  `popularity` and `available_markets` are not.
- Redirect URIs must use `127.0.0.1`, not `localhost`.

TIDAL (`openapi.tidal.com/v2`, JSON:API):
- Send `Accept` and `Content-Type: application/vnd.api+json`.
- List filters such as `filter[isrc]` and `filter[id]` are **repeated query
  parameters**, not comma-joined. `include` is comma-separated
  (`artists,albums`).
- **A request with several ISRCs returns only one track per ISRC**, often
  the single rather than the album version. Only a single-ISRC request
  returns every release (`TidalClient.all_versions`).
- Pagination uses `links.next`, which is a path relative to `/v2`.
- Adding items: `POST /playlists/{id}/relationships/items`, at most 50 per
  request, with `meta.onDuplicates: "SKIP"` and an `Idempotency-Key` header
  that stays the same across retries of that batch.
- New playlists use `attributes.name` (not `title`); `accessType` is
  `PUBLIC` or `UNLISTED`.
- TIDAL rate-limits bursts with HTTP 429; `ApiClient` handles it, so don't
  add extra sleeps.

When something about these APIs is unclear, check the live docs or test with
a read-only request; don't rely on memory.

## Testing

- Unit tests live in `tests/test_spotisync.py` and must not touch the
  network. Use `FakeSession` for HTTP and `FakeTidal` for sync logic.
- Add a test for every bug fix, ideally reproducing the real API behaviour
  (as the multi-ISRC test does).
- Commands that read the user's real accounts are fine for debugging when the
  user agrees. Anything that writes to TIDAL (`spotisync sync` without
  `--dry-run`) needs the user's explicit OK first.

## Secrets and commits

- Never commit `.env`, files from `~/.config/spotisync/`, or CSV reports
  (they can contain listening data). `.gitignore` covers the first and
  last; tokens live outside the repo.
- No client secrets exist or are needed: both services use PKCE with a
  client ID only.
- This repo is public. Commits must use the maintainer's GitHub noreply
  address (set in the repo's local git config), never a private email.
