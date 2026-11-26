"""What Spotisync put into each TIDAL playlist, so it can remove it again.

A TIDAL playlist can also hold tracks the user added by hand. Those must
survive a sync, so Spotisync only ever removes entries it recorded here as
its own. One small JSON file per Spotify playlist lives in
`~/.config/spotisync/state/`.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

from spotisync.auth import config_dir

log = logging.getLogger(__name__)

VERSION = 1


@dataclass(frozen=True)
class Record:
    """A Spotify playlist entry that Spotisync mirrored as a TIDAL track."""
    spotify_id: str | None
    isrc: str | None
    added_at: str | None    # when the entry was added on Spotify
    tidal_id: str           # the TIDAL track that represents it
    name: str = ""
    artists: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        return f"{self.name} — {', '.join(self.artists) or 'unknown artist'}"


@dataclass
class PlaylistState:
    spotify_playlist_id: str
    tidal_playlist_id: str
    records: list[Record] = field(default_factory=list)


class StateStore:
    def __init__(self, directory: Path | None = None):
        self.directory = directory or config_dir() / "state"

    def _path(self, spotify_playlist_id: str) -> Path:
        return self.directory / f"{spotify_playlist_id}.json"

    def load(self, spotify_playlist_id: str) -> PlaylistState | None:
        path = self._path(spotify_playlist_id)
        try:
            data = json.loads(path.read_text())
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as exc:
            # A broken file must not block syncing; without state nothing is
            # removed, which is the safe direction.
            log.warning("ignoring unreadable sync state %s: %s", path, exc)
            return None
        if data.get("version") != VERSION:
            log.warning("ignoring sync state %s with unknown version", path)
            return None
        return PlaylistState(
            spotify_playlist_id=data["spotify_playlist_id"],
            tidal_playlist_id=data["tidal_playlist_id"],
            records=[Record(**{**r, "artists": tuple(r.get("artists") or ())})
                     for r in data.get("records") or []],
        )

    def save(self, state: PlaylistState) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self._path(state.spotify_playlist_id)
        tmp = path.with_suffix(".tmp")
        payload = {
            "version": VERSION,
            "spotify_playlist_id": state.spotify_playlist_id,
            "tidal_playlist_id": state.tidal_playlist_id,
            "records": [asdict(r) for r in state.records],
        }
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(payload, fh, indent=1)
        os.replace(tmp, path)
