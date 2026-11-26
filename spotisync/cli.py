"""Command-line interface: `spotisync login|logout|sync`."""

from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from spotisync import __version__, spotify, tidal
from spotisync.auth import AuthError, TokenManager, config_dir
from spotisync.http import ApiError
from spotisync.sync import SyncResult, TrackResult, sync_playlist

log = logging.getLogger("spotisync")


@dataclass(frozen=True)
class Settings:
    spotify_client_id: str
    spotify_redirect_uri: str
    tidal_client_id: str
    tidal_redirect_uri: str
    tidal_scopes: tuple[str, ...]
    tidal_country_code: str | None

    @classmethod
    def from_env(cls) -> "Settings":
        try:
            from dotenv import load_dotenv
            load_dotenv()  # reads ./.env if present; real env vars win
        except ImportError:
            pass
        env = os.environ.get
        scopes = env("TIDAL_SCOPES", "").split() or list(tidal.DEFAULT_SCOPES)
        return cls(
            spotify_client_id=env("SPOTIFY_CLIENT_ID", "").strip(),
            spotify_redirect_uri=env("SPOTIFY_REDIRECT_URI", "").strip()
            or spotify.DEFAULT_REDIRECT_URI,
            tidal_client_id=env("TIDAL_CLIENT_ID", "").strip(),
            tidal_redirect_uri=env("TIDAL_REDIRECT_URI", "").strip()
            or tidal.DEFAULT_REDIRECT_URI,
            tidal_scopes=tuple(scopes),
            tidal_country_code=(env("TIDAL_COUNTRY_CODE", "").strip().upper() or None),
        )

    def require(self, service: str) -> None:
        var = {"spotify": "SPOTIFY_CLIENT_ID", "tidal": "TIDAL_CLIENT_ID"}[service]
        if not getattr(self, f"{service}_client_id"):
            raise SystemExit(f"error: {var} is not set. See README.md → Setup.")


def token_manager(settings: Settings, service: str, args: argparse.Namespace) -> TokenManager:
    settings.require(service)
    opts = {"open_browser": not args.no_browser}
    if service == "spotify":
        return spotify.make_token_manager(
            settings.spotify_client_id, settings.spotify_redirect_uri, **opts)
    return tidal.make_token_manager(
        settings.tidal_client_id, settings.tidal_redirect_uri, settings.tidal_scopes, **opts)


def services(arg: str) -> list[str]:
    return ["spotify", "tidal"] if arg == "all" else [arg]


# ── commands ─────────────────────────────────────────────────────────

def cmd_login(args: argparse.Namespace, settings: Settings) -> int:
    for service in services(args.service):
        token_manager(settings, service, args).login()
    print(f"Tokens are stored in {config_dir()}")
    return 0


def cmd_logout(args: argparse.Namespace, settings: Settings) -> int:
    for service in services(args.service):
        path = config_dir() / f"{service}-token.json"
        try:
            path.unlink()
            print(f"Removed {path}")
        except FileNotFoundError:
            print(f"No saved {service} token.")
    return 0


def print_progress(i: int, total: int, res: TrackResult) -> None:
    mark = {"isrc": "✓ isrc  ", "search": "✓ search"}.get(res.method or "", "✗ missing")
    print(f"  [{i:>{len(str(total))}}/{total}] {mark}  {res.track.label}")


def print_summary(result: SyncResult) -> None:
    pl = result.playlist
    total = len(result.results)
    by_isrc = sum(r.method == "isrc" for r in result.results)
    by_search = sum(r.method == "search" for r in result.results)
    verb = "would add" if result.dry_run else "added"
    print()
    print(f"  Spotify playlist : {pl.name}  ({total} tracks, {pl.skipped} skipped: local/episodes)")
    if result.tidal_playlist_id:
        state = "new" if result.created else "existing"
        print(f"  TIDAL playlist   : https://tidal.com/playlist/{result.tidal_playlist_id} ({state})")
    elif result.dry_run:
        print("  TIDAL playlist   : (would be created)")
    print(f"  Matched          : {len(result.matched)}/{total} "
          f"({by_isrc} by ISRC, {by_search} by search)")
    print(f"  Tracks {verb:<10}: {result.added}  ({result.already_present} already there)")
    for warning in result.warnings:
        print(f"  ! {warning}")
    if result.unmatched:
        print(f"  Not found on TIDAL ({len(result.unmatched)}):")
        for r in result.unmatched:
            isrc = f"  [ISRC {r.track.isrc}]" if r.track.isrc else ""
            print(f"    - {r.track.label}{isrc}")


def write_report(path: Path, results: list[SyncResult]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["spotify_playlist", "track", "artists", "isrc", "spotify_url",
                         "tidal_id", "method", "score"])
        for sync in results:
            for r in sync.results:
                writer.writerow([
                    sync.playlist.name, r.track.name, "; ".join(r.track.artists),
                    r.track.isrc or "",
                    f"https://open.spotify.com/track/{r.track.spotify_id}" if r.track.spotify_id else "",
                    r.tidal_id or "", r.method or "",
                    f"{r.score:.2f}" if r.score is not None else "",
                ])


def cmd_sync(args: argparse.Namespace, settings: Settings) -> int:
    sp = spotify.SpotifyClient(token_manager(settings, "spotify", args))
    td = tidal.TidalClient(token_manager(settings, "tidal", args),
                           country_code=settings.tidal_country_code)
    access = "PUBLIC" if args.public else "UNLISTED"
    results: list[SyncResult] = []
    failures = 0

    for ref in args.playlists:
        try:
            playlist = sp.get_playlist(ref)
        except (ValueError, spotify.SpotifyAccessError) as exc:
            print(f"\n✗ {ref}: {exc}", file=sys.stderr)
            failures += 1
            continue
        print(f"\n▶ {playlist.name}: matching {len(playlist.tracks)} tracks "
              f"on TIDAL ({td.country_code})")
        result = sync_playlist(td, playlist, dry_run=args.dry_run, always_new=args.new,
                               access_type=access,
                               progress=None if args.quiet else print_progress)
        print_summary(result)
        results.append(result)

    if args.report and results:
        write_report(args.report, results)
        print(f"\nReport written to {args.report}")
    if args.dry_run:
        print("\nDry run: nothing was changed on TIDAL.")
    return 1 if failures else 0


# ── entry point ──────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="spotisync", description="Copy Spotify playlists to TIDAL.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    parser.add_argument("--no-browser", action="store_true",
                        help="print login URLs instead of opening a browser")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("login", help="log in to Spotify and/or TIDAL")
    p.add_argument("service", nargs="?", default="all", choices=["all", "spotify", "tidal"])
    p.set_defaults(func=cmd_login)

    p = sub.add_parser("logout", help="forget saved tokens")
    p.add_argument("service", nargs="?", default="all", choices=["all", "spotify", "tidal"])
    p.set_defaults(func=cmd_logout)

    p = sub.add_parser("sync", help="copy one or more playlists to TIDAL")
    p.add_argument("playlists", nargs="+", metavar="PLAYLIST",
                   help="Spotify playlist URL, URI or ID")
    p.add_argument("-n", "--dry-run", action="store_true",
                   help="match tracks and show what would change, without writing")
    p.add_argument("--new", action="store_true",
                   help="always create a new TIDAL playlist instead of updating one "
                        "with the same name")
    p.add_argument("--public", action="store_true",
                   help="make newly created TIDAL playlists public (default: unlisted)")
    p.add_argument("--report", type=Path, metavar="FILE.csv",
                   help="write a CSV with every track and how it was matched")
    p.add_argument("-q", "--quiet", action="store_true", help="hide per-track lines")
    p.set_defaults(func=cmd_sync)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        return args.func(args, Settings.from_env())
    except AuthError as exc:
        print(f"error: authentication failed: {exc}", file=sys.stderr)
    except ApiError as exc:
        print(f"error: {exc}", file=sys.stderr)
        if exc.status == 403:
            print("hint: check that your TIDAL/Spotify app has the scopes listed in "
                  "README.md and run `spotisync login` again.", file=sys.stderr)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    return 1
