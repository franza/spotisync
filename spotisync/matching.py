"""Decide whether a TIDAL track is the same recording as a Spotify track."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

from spotisync.spotify import Track
from spotisync.tidal import TidalTrack

# A fuzzy match must score at least this (0..1) to be accepted.
MIN_SCORE = 0.80
MIN_TITLE_SIMILARITY = 0.80
MAX_DURATION_DIFF_S = 7.0

_FEAT_RE = re.compile(r"[\(\[]\s*(?:feat|ft|featuring|with)\.?\s[^\)\]]*[\)\]]|\s(?:feat|ft|featuring)\.?\s.*$")
_REMASTER_RE = re.compile(
    r"\s*(?:[\(\[][^\)\]]*remaster[^\)\]]*[\)\]]|-\s*(?:\d{4}\s+)?remaster(?:ed)?(?:\s+\d{4})?(?:\s+version)?\s*$)"
)
_NON_WORD_RE = re.compile(r"[^\w\s]")
_SPACES_RE = re.compile(r"\s+")


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    return "".join(c for c in text if not unicodedata.combining(c)).lower()


def normalize_title(title: str) -> str:
    """Lowercase, strip accents, 'feat.' credits and remaster tags, punctuation."""
    text = _fold(title)
    text = _FEAT_RE.sub("", text)
    text = _REMASTER_RE.sub("", text)
    text = text.replace("&", " and ")
    text = _NON_WORD_RE.sub(" ", text)
    return _SPACES_RE.sub(" ", text).strip()


def normalize_artist(name: str) -> str:
    text = _fold(name).replace("&", " and ")
    text = _NON_WORD_RE.sub(" ", text)
    text = _SPACES_RE.sub(" ", text).strip()
    return text[4:] if text.startswith("the ") else text


def title_similarity(spotify: Track, tidal: TidalTrack) -> float:
    target = normalize_title(spotify.name)
    best = 0.0
    for candidate in {tidal.title, tidal.full_title}:
        best = max(best, SequenceMatcher(None, target, normalize_title(candidate)).ratio())
    return best


def artist_score(spotify: Track, tidal: TidalTrack) -> float:
    """1.0 if the primary artist matches, else the share of artists in common."""
    ours = [normalize_artist(a) for a in spotify.artists]
    theirs = {normalize_artist(a) for a in tidal.artists}
    if not ours or not theirs:
        return 0.0
    if ours[0] in theirs:
        return 1.0
    return len(set(ours) & theirs) / len(ours)


def album_score(spotify: Track, tidal: TidalTrack) -> float:
    """How well the release matches: album title similarity, plus a little for
    the same release type (album vs single). 0 when either side is unknown."""
    if not spotify.album or not tidal.album:
        return 0.0
    title = SequenceMatcher(
        None, normalize_title(spotify.album), normalize_title(tidal.album)
    ).ratio()
    same_type = bool(spotify.album_type) and _release_kind(spotify.album_type) == _release_kind(tidal.album_type)
    return title + (0.1 if same_type else 0.0)


def same_album(spotify: Track, tidal: TidalTrack) -> bool:
    if not spotify.album or not tidal.album:
        return False
    return normalize_title(spotify.album) == normalize_title(tidal.album)


def _release_kind(album_type: str | None) -> str | None:
    # Spotify says "single" for EPs as well; TIDAL distinguishes EP.
    kind = (album_type or "").upper()
    return "SINGLE" if kind in ("SINGLE", "EP") else kind or None


def duration_diff(spotify: Track, tidal: TidalTrack) -> float | None:
    if spotify.duration_ms is None or tidal.duration_s is None:
        return None
    return abs(spotify.duration_ms / 1000 - tidal.duration_s)


def score(spotify: Track, tidal: TidalTrack) -> float:
    """Weighted similarity in 0..1; 0 when a hard requirement fails."""
    title = title_similarity(spotify, tidal)
    artists = artist_score(spotify, tidal)
    diff = duration_diff(spotify, tidal)
    if title < MIN_TITLE_SIMILARITY or artists < 0.5:
        return 0.0
    if diff is not None and diff > MAX_DURATION_DIFF_S:
        return 0.0
    duration = 0.5 if diff is None else 1.0 - diff / MAX_DURATION_DIFF_S
    return 0.5 * title + 0.35 * artists + 0.15 * duration


@dataclass(frozen=True)
class Match:
    track: TidalTrack
    score: float


def best_fuzzy_match(spotify: Track, candidates: list[TidalTrack]) -> Match | None:
    scored = [Match(c, score(spotify, c)) for c in candidates]
    scored = [m for m in scored if m.score >= MIN_SCORE]
    # The album only breaks near-ties: it picks the album version over the
    # single when both match equally well, but never rescues a bad match.
    return max(scored, key=lambda m: (round(m.score, 2), album_score(spotify, m.track)),
               default=None)


def best_isrc_match(spotify: Track, candidates: list[TidalTrack]) -> TidalTrack | None:
    """An ISRC identifies the recording, so any candidate is acceptable.

    Several TIDAL tracks can share one ISRC (album, single, compilation).
    Prefer the one from the same album as on Spotify, then the one that
    otherwise looks most like the Spotify track.
    """
    if not candidates:
        return None
    return max(candidates, key=lambda c: (
        album_score(spotify, c),
        artist_score(spotify, c),
        title_similarity(spotify, c),
        -(duration_diff(spotify, c) or 0.0),
    ))


def search_query(spotify: Track) -> str:
    artist = spotify.artists[0] if spotify.artists else ""
    title = normalize_title(spotify.name) or spotify.name
    return f"{artist} {title}".strip()
