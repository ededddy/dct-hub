"""OIDC login against Keycloak (or any conformant provider).

Hand-rolled authorization-code + PKCE flow over an injectable httpx client;
joserfc handles ID-token signature validation against the provider's JWKS.
Discovery is lazy and cached, so the hub starts even when the IdP is down.
The JWKS is refetched once on validation failure to survive key rotation.
"""

import base64
import hashlib
import logging
import secrets
import time
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
from joserfc import errors as jose_errors
from joserfc import jwt as jose_jwt
from joserfc.jwk import KeySet

from .config import OidcConfig

logger = logging.getLogger("dct_hub.auth")


class OidcClient:
    def __init__(self, config: OidcConfig, http: httpx.AsyncClient | None = None):
        self.config = config
        self._http = http
        self._metadata: dict | None = None
        self._jwks = None

    async def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=10)
        return self._http

    async def metadata(self) -> dict:
        if self._metadata is None:
            http = await self._client()
            url = self.config.issuer.rstrip("/") + "/.well-known/openid-configuration"
            resp = await http.get(url)
            if resp.status_code != 200:
                raise HTTPException(status_code=502, detail=f"OIDC discovery failed: HTTP {resp.status_code}")
            self._metadata = resp.json()
        return self._metadata

    async def jwks(self):
        if self._jwks is None:
            http = await self._client()
            meta = await self.metadata()
            resp = await http.get(meta["jwks_uri"])
            if resp.status_code != 200:
                raise HTTPException(status_code=502, detail="JWKS fetch failed")
            self._jwks = KeySet.import_key_set(resp.json())
        return self._jwks

    async def authorize_url(self, redirect_uri: str, txn: dict) -> str:
        meta = await self.metadata()
        params = {
            "response_type": "code",
            "client_id": self.config.client_id,
            "redirect_uri": redirect_uri,
            "scope": self.config.scopes,
            "state": txn["state"],
            "nonce": txn["nonce"],
            "code_challenge": txn["challenge"],
            "code_challenge_method": "S256",
        }
        return meta["authorization_endpoint"] + "?" + urlencode(params)

    async def exchange(self, code: str, redirect_uri: str, verifier: str) -> dict:
        meta = await self.metadata()
        http = await self._client()
        resp = await http.post(
            meta["token_endpoint"],
            data={
                "grant_type": "authorization_code",
                "client_id": self.config.client_id,
                "client_secret": self.config.client_secret,
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": verifier,
            },
        )
        if resp.status_code != 200:
            raise HTTPException(status_code=502, detail="token exchange failed")
        return resp.json()

    async def validate_id_token(self, id_token: str, nonce: str) -> dict:
        token = None
        for attempt in (0, 1):
            try:
                token = jose_jwt.decode(id_token, await self.jwks())
                break
            except HTTPException:
                raise  # JWKS fetch failures (502) must not be retried as 401
            except Exception as exc:
                self._jwks = None  # signing-key rotation: refetch once
                if attempt == 1:
                    raise HTTPException(status_code=401, detail="invalid ID token") from exc
        claims = token.claims
        try:
            jose_jwt.JWTClaimsRegistry(now=int(time.time()), leeway=30).validate(claims)
        except (jose_errors.JoseError, ValueError) as exc:
            raise HTTPException(status_code=401, detail="ID token claims invalid or expired") from exc

        if claims.get("iss") != self.config.issuer.rstrip("/") and claims.get("iss") != self.config.issuer:
            raise HTTPException(status_code=401, detail="bad issuer")
        aud = claims.get("aud") or []
        if isinstance(aud, str):
            aud = [aud]
        if self.config.client_id not in aud:
            raise HTTPException(status_code=401, detail="bad audience")
        if claims.get("nonce") != nonce:
            raise HTTPException(status_code=401, detail="bad nonce")
        return dict(claims)


def new_txn(next_url: str) -> dict:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return {
        "state": secrets.token_urlsafe(24),
        "nonce": secrets.token_urlsafe(24),
        "verifier": verifier,
        "challenge": challenge,
        "next": next_url,
    }


def _safe_next(next_url: str) -> str:
    if next_url.startswith("/") and not next_url.startswith("//"):
        return next_url
    return "/"


def register_auth_routes(app: FastAPI, oidc: OidcClient, groups_claim: str) -> None:
    @app.get("/auth/login", include_in_schema=False)
    async def login(request: Request, next: str = "/") -> RedirectResponse:
        txn = new_txn(_safe_next(next))
        request.session["oidc_txn"] = txn
        redirect_uri = str(request.url_for("oidc_callback"))
        return RedirectResponse(await oidc.authorize_url(redirect_uri, txn))

    @app.get("/auth/callback", include_in_schema=False, name="oidc_callback")
    async def oidc_callback(request: Request, code: str = "", state: str = "") -> RedirectResponse:
        txn = request.session.pop("oidc_txn", None)
        if not txn or not state or state != txn["state"]:
            logger.warning("OIDC callback with bad or missing state")
            raise HTTPException(status_code=400, detail="bad OAuth state")
        try:
            tokens = await oidc.exchange(code, str(request.url_for("oidc_callback")), txn["verifier"])
            claims = await oidc.validate_id_token(tokens["id_token"], txn["nonce"])
        except HTTPException as exc:
            logger.warning("OIDC callback failed: %s", exc.detail)
            raise
        groups = claims.get(groups_claim) or []
        if isinstance(groups, str):
            groups = [groups]
        request.session["user"] = {
            "sub": claims.get("sub", ""),
            "name": claims.get("name") or claims.get("preferred_username") or "",
            "email": claims.get("email", ""),
            "groups": groups,
        }
        logger.info("login: %s", claims.get("sub", ""))
        return RedirectResponse(txn["next"])

    @app.get("/auth/logout", include_in_schema=False)
    async def logout(request: Request) -> RedirectResponse:
        user = request.session.get("user") or {}
        logger.info("logout: %s", user.get("sub", ""))
        request.session.clear()
        return RedirectResponse("/")

    @app.get("/auth/me", include_in_schema=False)
    async def me(request: Request) -> dict:
        return {"user": request.session.get("user")}
