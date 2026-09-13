# SPDX-License-Identifier: Apache-2.0
"""OAuth 2 « application installée » and token storage (spec §8).

Two server instances (chat + Cowork) share ``token.json``. A refresh is serialised with a
file lock; before refreshing, the file is re-read in case the other instance already did
it. Writes are atomic (temp file + rename) and ``0600``.
"""

from __future__ import annotations

import fcntl
import os
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol, cast

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow  # pyright: ignore[reportMissingTypeStubs]

SCOPES: list[str] = [
    "https://www.googleapis.com/auth/calendar.events.owned",
    "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
    "https://www.googleapis.com/auth/tasks",
]


class AuthError(RuntimeError):
    """No usable token, or the refresh failed."""


class OAuthCredentials(Protocol):
    """The subset of ``google.oauth2.credentials.Credentials`` we rely on (typed façade)."""

    token: str | None
    refresh_token: str | None

    @property
    def valid(self) -> bool: ...
    def refresh(self, request: Request) -> None: ...
    def to_json(self, strip: Sequence[str] | None = None) -> str: ...


class TokenStore:
    def __init__(self, client_secret: Path, token_path: Path) -> None:
        self._client_secret = client_secret
        self._token_path = token_path
        self._lock_path = token_path.with_name("token.lock")
        self._creds: OAuthCredentials | None = None

    # -- one-off interactive flow (CLI) -------------------------------------------------

    def authorize(self) -> None:
        """Run the browser consent flow and persist the resulting token."""
        if not self._client_secret.is_file():
            raise AuthError(f"client_secret.json introuvable : {self._client_secret}")
        flow = InstalledAppFlow.from_client_secrets_file(  # pyright: ignore[reportUnknownMemberType]
            str(self._client_secret), SCOPES
        )
        creds = cast(
            OAuthCredentials,
            flow.run_local_server(port=0, access_type="offline", prompt="consent"),  # pyright: ignore[reportUnknownMemberType]
        )
        if not creds.refresh_token:
            raise AuthError(
                "Google n'a pas renvoyé de refresh token ; révoquer l'accès de l'application "
                "dans le compte Google puis relancer `loom-plan auth`."
            )
        with self._locked():
            self._write(creds)
        self._creds = creds

    # -- runtime -----------------------------------------------------------------------

    def access_token(self) -> str:
        """Return a valid access token, refreshing (under lock) if needed."""
        creds = self._creds or self._read()
        if creds.valid:
            self._creds = creds
            return _token(creds)
        with self._locked():
            creds = self._read()  # the other instance may already have refreshed
            if not creds.valid:
                try:
                    creds.refresh(Request())
                except Exception as exc:  # google.auth raises several exception types
                    raise AuthError(f"échec du refresh du token : {exc}") from exc
                self._write(creds)
            self._creds = creds
        return _token(creds)

    def invalidate(self) -> None:
        """Forget the in-memory credentials (call after a 401)."""
        self._creds = None

    @property
    def token_path(self) -> Path:
        return self._token_path

    # -- internals ---------------------------------------------------------------------

    def _read(self) -> OAuthCredentials:
        if not self._token_path.is_file():
            raise AuthError(f"aucun token ({self._token_path}) : lancer `loom-plan auth` d'abord.")
        return cast(
            OAuthCredentials,
            Credentials.from_authorized_user_file(str(self._token_path), SCOPES),  # pyright: ignore[reportUnknownMemberType]
        )

    def _write(self, creds: OAuthCredentials) -> None:
        tmp = self._token_path.with_name(self._token_path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(creds.to_json())
        os.replace(tmp, self._token_path)

    @contextmanager
    def _locked(self) -> Generator[None]:
        self._token_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def _token(creds: OAuthCredentials) -> str:
    if creds.token is None:
        raise AuthError("credentials valides mais sans access token (état inattendu)")
    return creds.token
