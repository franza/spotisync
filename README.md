# Spotisync

Keep TIDAL playlists in sync with Spotify playlists. Tracks are matched by
**ISRC** (the recording's international ID) and, when that fails, by a search
that checks title, artist and duration.

Spotify is the source: each sync adds tracks that are new on Spotify and
removes tracks that were deleted there. Tracks you added to the TIDAL playlist
yourself are never removed, and it never creates duplicates.

```
$ spotisync sync https://open.spotify.com/playlist/3cEYpjA9oz9GiPac4AsH4n

▶ Road Trip: matching 3 tracks on TIDAL (DE)
  [1/3] ✓ isrc    Hey Jude - Remastered 2015 — The Beatles
  [2/3] ✓ search  Intro — The xx
  [3/3] ✗ missing Some Bootleg — Unknown Band

  Spotify playlist : Road Trip  (3 tracks, 0 skipped: local/episodes)
  TIDAL playlist   : https://tidal.com/playlist/1b2c… (new)
  Matched          : 2/3 (1 by ISRC, 1 by search)
  Tracks added     : 2  (0 already there)
  Not found on TIDAL (1):
    - Some Bootleg — Unknown Band
```

---

## 1. Requirements

| | |
|---|---|
| Python | 3.10 or newer (`python3 --version`) |
| Spotify | An account with **Premium**. Since February 2026, Spotify apps in Development Mode only work while their owner has Premium. |
| TIDAL | A TIDAL account (a subscription is needed to play the result) |

> **Which Spotify playlists can be read?** Since February 2026, Spotify only
> lets Development Mode apps read the tracks of playlists that **you own or
> collaborate on**. Playlists made by other people or by Spotify itself
> (editorial and algorithmic ones) can't be synced.

## 2. Install

```bash
cd spotisync
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e .
spotisync --version
```

`pip install -e .` installs the two dependencies (`requests` and
`python-dotenv`) and puts a `spotisync` command on your PATH while the venv
is active. You can also run it as `python -m spotisync`.

## 3. Create a Spotify app (one time)

1. Open <https://developer.spotify.com/dashboard> and log in.
2. Click **Create app**.
   - **App name / description**: anything, e.g. `spotisync`.
   - **Redirect URIs**: add exactly `http://127.0.0.1:8888/callback`.
     Spotify does not accept `localhost`; it must be `127.0.0.1`.
   - **Which API/SDKs**: tick **Web API**.
   - Accept the terms and click **Save**.
3. Open the app's **Settings** and copy the **Client ID**. You do **not** need
   the client secret, because Spotisync logs in with PKCE.
4. If another Spotify account (not the app owner) will use the app, add it
   under **User Management**. Development Mode allows a few named users.

## 4. Create a TIDAL app (one time)

1. Open <https://developer.tidal.com/dashboard> and log in with your TIDAL
   account.
2. Click **Create new app** and give it a name, e.g. `spotisync`.
3. In the app's settings:
   - **Redirect URIs**: add exactly `http://localhost:8889/callback`.
   - **Scopes**: enable `user.read`, `playlists.read`, `playlists.write` and
     `search.read`.
4. Copy the **Client ID**. The client secret is not needed.

## 5. Configure

```bash
cp .env.example .env
```

Edit `.env` and paste in the two client IDs:

```dotenv
SPOTIFY_CLIENT_ID=0123456789abcdef0123456789abcdef
TIDAL_CLIENT_ID=AbCdEfGhIjKlMnOp
```

`.env` is read from the directory you run `spotisync` in. Exported environment
variables work too and take precedence. Optional settings:

| Variable | Default | Purpose |
|---|---|---|
| `SPOTIFY_REDIRECT_URI` | `http://127.0.0.1:8888/callback` | Must match the Spotify dashboard exactly |
| `TIDAL_REDIRECT_URI` | `http://localhost:8889/callback` | Must match the TIDAL dashboard exactly |
| `TIDAL_COUNTRY_CODE` | your TIDAL account's country | Catalog region used for matching |
| `TIDAL_SCOPES` | `user.read playlists.read playlists.write search.read` | Space-separated |
| `SPOTISYNC_CONFIG_DIR` | `~/.config/spotisync` | Where login tokens and sync records are saved |

## 6. Log in

```bash
spotisync login
```

Your browser opens Spotify's consent page, then TIDAL's. Approve both. The
script listens on the redirect port, picks up the authorisation code and saves
the tokens to `~/.config/spotisync/` (file mode `0600`). Tokens refresh
automatically, so you normally log in only once. Use `spotisync login tidal`
to redo one service and `spotisync logout` to delete the saved tokens.

If you skip this step, `spotisync sync` asks you to log in the first time it
needs to.

## 7. Sync

```bash
# Preview: match everything, change nothing.
# Keep the single quotes: zsh treats the "?" in Spotify's share links as a
# wildcard and fails with "zsh: no matches found" without them.
spotisync sync --dry-run 'https://open.spotify.com/playlist/3cEYpjA9oz9GiPac4AsH4n?si=0123456789abcdef'

# Do it. A URL, a spotify:playlist:… URI or a bare ID all work; a bare ID
# needs no quotes.
spotisync sync 3cEYpjA9oz9GiPac4AsH4n

# Several playlists, plus a CSV of every track and how it matched
spotisync sync 'PLAYLIST_URL_1' 'PLAYLIST_URL_2' --report matches.csv
```

| Option | Effect |
|---|---|
| `-n`, `--dry-run` | Match and report, but don't create or change anything on TIDAL |
| `--new` | Always create a new TIDAL playlist, even if one with the same name exists |
| `--keep-removed` | Only add tracks; keep tracks that were deleted on Spotify |
| `--public` | Make newly created playlists public (default: unlisted) |
| `--report FILE.csv` | Write every track, its TIDAL ID and the match method to a CSV file |
| `-q`, `--quiet` | Hide the per-track lines |
| `--no-browser` | Print login URLs instead of opening a browser |
| `-v`, `--verbose` | Debug logging, including HTTP retries |

---

## How it works

1. **Read** the Spotify playlist (`GET /playlists/{id}/items`, 50 per page).
   Local files and podcast episodes are skipped.
2. **Look up ISRCs** on TIDAL in batches of 20 (`GET /tracks?filter[isrc]=…`).
   The same recording often exists on TIDAL as several releases (album,
   single, compilation) with one ISRC, and a batched lookup returns only one
   of them. If that one isn't from the album the Spotify track comes from,
   Spotisync looks that ISRC up on its own, which returns every release, and
   picks the release from the same album.
3. **Search** for tracks with no ISRC hit. A candidate is accepted only if
   all of these hold:
   - the title is at least 80% similar after normalisation ("Remastered" and
     "feat." are ignored),
   - the primary artist matches,
   - the duration is within 7 seconds.

   Otherwise the track is reported as missing rather than guessed.
4. **Find or create** the TIDAL playlist. Spotisync uses the playlist it
   synced to last time, even if you renamed the Spotify playlist. Otherwise it
   looks for one of yours with the exact same name, and if there is none, it
   creates one. The playlist is only created *after* matching, so a failure
   never leaves an empty playlist behind.
5. **Add** the missing tracks at their Spotify position: each one goes in
   front of the track that follows it on Spotify, so a song re-added at the
   top of the Spotify playlist also lands at the top on TIDAL. Tracks already
   in the TIDAL playlist are never moved, so if you reorder songs on Spotify,
   TIDAL keeps its order. Requests use TIDAL's `onDuplicates: SKIP` and an
   idempotency key, so retries never add a track twice.
6. **Remove** tracks that were deleted from the Spotify playlist since the
   last sync. Use `--keep-removed` to skip this step.

### What gets removed

Spotisync remembers which TIDAL tracks it put into each playlist, in
`~/.config/spotisync/state/` (one small file per Spotify playlist). Only
those can ever be removed:

- **Deleted on Spotify:** removed from TIDAL on the next sync.
- **Added by hand on TIDAL:** never removed, because Spotisync didn't add it.
- **Still listed on Spotify, but without track details** (the song was taken
  down or isn't available in your region): kept, because you didn't delete it.
  It is removed once you delete the entry on Spotify.
- **Spotify returns an empty playlist:** nothing is removed, in case it's an
  API glitch.

The first sync of a playlist with this version only records what is already
there and removes nothing. Removals start with the next sync. Run with
`--dry-run` first to see exactly what would be removed.

HTTP 429 and 5xx responses are retried with backoff (honouring
`Retry-After`), and expired tokens are refreshed automatically.

### Limitations

- The record of what Spotisync added is stored on the computer that ran the
  sync. Syncing the same playlist from a second computer adds tracks
  normally, but only removes tracks once that computer has synced it once.
- Only playlists you own or collaborate on can be read (Spotify's rule, see
  above).
- Some recordings simply aren't on TIDAL in your region. Those are listed
  under "Not found on TIDAL" and in the `--report` CSV.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Spotify page says **INVALID_CLIENT: Invalid redirect URI** | The dashboard redirect URI must be exactly `http://127.0.0.1:8888/callback` (or whatever `SPOTIFY_REDIRECT_URI` is). |
| TIDAL login page shows a redirect or client error | Check the redirect URI and the four scopes in the TIDAL dashboard. |
| `Spotify refused access to this playlist's tracks` | You don't own or collaborate on that playlist, so Spotify won't let the app read it (see Requirements). |
| `HTTP 403` from TIDAL when creating a playlist | `playlists.write` isn't enabled for your TIDAL app, or the token predates it. Enable it, then run `spotisync login tidal`. |
| `Could not listen on 127.0.0.1:8888` | Another program uses the port. The script falls back to asking you to paste the redirected URL. You can also change the port in both the dashboard and `.env`. |
| Running on a remote machine over SSH | Forward the callback ports: `ssh -L 8888:127.0.0.1:8888 -L 8889:127.0.0.1:8889 host`, then run with `--no-browser` and open the printed URLs locally. |
| Many `HTTP 429 … retrying` lines | TIDAL is rate-limiting. This is handled automatically, it is just slower. |

## Development

```bash
pip install -e '.[dev]'
pytest
```

Code layout:

| Module | Responsibility |
|---|---|
| `spotisync/auth.py` | OAuth PKCE login, local callback server, token cache and refresh |
| `spotisync/http.py` | JSON API client with retries, backoff and 401 refresh |
| `spotisync/spotify.py` | Read Spotify playlists |
| `spotisync/tidal.py` | TIDAL catalog lookup, search and playlist writes |
| `spotisync/matching.py` | Title and artist normalisation, scoring |
| `spotisync/sync.py` | Orchestration: match, then find or create, then add |
| `spotisync/cli.py` | Command-line interface |

## License

MIT
