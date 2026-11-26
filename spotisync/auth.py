"""OAuth 2.0 authorization-code + PKCE login with a local callback server.

Both Spotify and TIDAL support PKCE for public clients, so no client secret
is needed or stored. Tokens are cached on disk (mode 0600) and refreshed
automatically.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import threading
import time
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import requests

log = logging.getLogger(__name__)

LOGIN_TIMEOUT_S = 300
EXPIRY_MARGIN_S = 60


class AuthError(Exception):
    pass


@dataclass(frozen=True)
class OAuthConfig:
    service: str            # "spotify" or "tidal"; also names the cache file
    client_id: str
    authorize_url: str
    token_url: str
    redirect_uri: str
    scopes: tuple[str, ...]


def config_dir() -> Path:
    if custom := os.environ.get("SPOTISYNC_CONFIG_DIR"):
        return Path(custom).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "spotisync"


def make_pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) using the S256 method."""
    verifier = secrets.token_urlsafe(64)[:128]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


class TokenManager:
    """Hand out a valid access token, logging in or refreshing as needed."""

    def __init__(self, config: OAuthConfig, *, interactive: bool = True,
                 open_browser: bool = True, cache_dir: Path | None = None):
        self.config = config
        self.interactive = interactive
        self.open_browser = open_browser
        self.cache_path = (cache_dir or config_dir()) / f"{config.service}-token.json"
        self._token: dict | None = None
        self._lock = threading.Lock()

    # ── public API ────────────────────────────────────────────────────

    def access_token(self) -> str:
        with self._lock:
            token = self._token or self._load()
            if token and token.get("expires_at", 0) - EXPIRY_MARGIN_S > time.time():
                self._token = token
                return token["access_token"]
            if token and token.get("refresh_token"):
                try:
                    return self._refresh(token)["access_token"]
                except AuthError as exc:
                    log.warning("%s token refresh failed (%s); logging in again",
                                self.config.service, exc)
            return self._login()["access_token"]

    def force_refresh(self) -> str:
        with self._lock:
            token = self._token or self._load()
            if token and token.get("refresh_token"):
                try:
                    return self._refresh(token)["access_token"]
                except AuthError:
                    pass
            return self._login()["access_token"]

    def login(self) -> None:
        """Run the interactive login now, replacing any cached token."""
        with self._lock:
            self._login()

    def logout(self) -> bool:
        self._token = None
        try:
            self.cache_path.unlink()
            return True
        except FileNotFoundError:
            return False

    # ── cache ────────────────────────────────────────────────────────

    def _load(self) -> dict | None:
        try:
            data = json.loads(self.cache_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return None
        # A token issued to another app or with other scopes is no good.
        if data.get("client_id") != self.config.client_id:
            return None
        if set(data.get("scopes", [])) != set(self.config.scopes):
            return None
        return data

    def _save(self, token: dict) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cache_path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(token, fh)
        os.replace(tmp, self.cache_path)
        self._token = token

    # ── token endpoint ───────────────────────────────────────────────

    def _token_request(self, form: dict[str, str]) -> dict:
        try:
            resp = requests.post(self.config.token_url, data=form, timeout=30)
        except requests.RequestException as exc:
            raise AuthError(f"token request failed: {exc}") from exc
        if not resp.ok:
            raise AuthError(f"token endpoint returned HTTP {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    def _store_response(self, payload: dict, previous_refresh: str | None = None) -> dict:
        if "access_token" not in payload:
            raise AuthError(f"no access_token in token response: {payload}")
        token = {
            "access_token": payload["access_token"],
            # Refresh responses may omit refresh_token; keep the old one then.
            "refresh_token": payload.get("refresh_token") or previous_refresh,
            "expires_at": time.time() + float(payload.get("expires_in", 3600)),
            "client_id": self.config.client_id,
            "scopes": list(self.config.scopes),
        }
        self._save(token)
        return token

    def _refresh(self, token: dict) -> dict:
        log.debug("refreshing %s token", self.config.service)
        payload = self._token_request({
            "grant_type": "refresh_token",
            "refresh_token": token["refresh_token"],
            "client_id": self.config.client_id,
        })
        return self._store_response(payload, token["refresh_token"])

    # ── interactive login ────────────────────────────────────────────

    def _login(self) -> dict:
        if not self.interactive:
            raise AuthError(
                f"not logged in to {self.config.service}; run `spotisync login {self.config.service}`"
            )
        verifier, challenge = make_pkce_pair()
        state = secrets.token_urlsafe(16)
        url = self.config.authorize_url + "?" + urlencode({
            "client_id": self.config.client_id,
            "response_type": "code",
            "redirect_uri": self.config.redirect_uri,
            "scope": " ".join(self.config.scopes),
            "code_challenge_method": "S256",
            "code_challenge": challenge,
            "state": state,
        })
        params = self._wait_for_callback(url)
        if "error" in params:
            raise AuthError(f"{self.config.service} login was refused: {params['error']}")
        if params.get("state") != state:
            raise AuthError("OAuth state mismatch; possible CSRF, aborting")
        if not params.get("code"):
            raise AuthError("no authorization code in the callback")
        payload = self._token_request({
            "grant_type": "authorization_code",
            "code": params["code"],
            "redirect_uri": self.config.redirect_uri,
            "client_id": self.config.client_id,
            "code_verifier": verifier,
        })
        print(f"Logged in to {self.config.service.capitalize()}.")
        return self._store_response(payload)

    def _wait_for_callback(self, auth_url: str) -> dict[str, str]:
        redirect = urlparse(self.config.redirect_uri)
        host = redirect.hostname or "127.0.0.1"
        if host == "localhost":
            host = "127.0.0.1"
        port = redirect.port or 80
        expected_path = redirect.path or "/"
        result: dict[str, str] = {}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 (http.server API)
                parsed = urlparse(self.path)
                if parsed.path != expected_path:
                    self.send_error(404)
                    return
                result.update({k: v[0] for k, v in parse_qs(parsed.query).items()})
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(
                    b"<html><body style='font-family:sans-serif'>"
                    b"<h2>Spotisync: login complete.</h2>"
                    b"<p>You can close this tab and return to the terminal.</p>"
                    b"</body></html>"
                )

            def log_message(self, *args):  # keep the terminal quiet
                pass

        name = self.config.service.capitalize()
        try:
            server = HTTPServer((host, port), Handler)
        except OSError as exc:
            # Port taken or not bindable: fall back to copy-paste.
            print(f"\nCould not listen on {host}:{port} ({exc}).")
            print(f"Open this URL to log in to {name}:\n\n  {auth_url}\n")
            pasted = input("After approving, paste the full URL your browser was sent to: ")
            return {k: v[0] for k, v in parse_qs(urlparse(pasted.strip()).query).items()}

        print(f"\nLog in to {name} in your browser. If it does not open, visit:\n\n  {auth_url}\n")
        if self.open_browser:
            webbrowser.open(auth_url)
        server.timeout = 1.0
        deadline = time.time() + LOGIN_TIMEOUT_S
        try:
            while not result and time.time() < deadline:
                server.handle_request()
        finally:
            server.server_close()
        if not result:
            raise AuthError(f"timed out waiting for the {name} login callback")
        return result
