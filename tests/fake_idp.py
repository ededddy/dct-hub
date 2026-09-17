"""In-process fake Keycloak for tests: discovery, authorize (auto-approves),
token (issues RS256-signed id_tokens), and JWKS endpoints."""

import time
from urllib.parse import parse_qs, urlencode, urlparse

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from joserfc import jwt as jose_jwt
from joserfc.jwk import RSAKey
from starlette.applications import Starlette
from starlette.responses import JSONResponse, RedirectResponse
from starlette.routing import Route

ISSUER = "http://idp.test/realms/test"
CLIENT_ID = "dct-hub"


class FakeIdP:
    def __init__(self, issuer: str = ISSUER, client_id: str = CLIENT_ID):
        self.issuer = issuer
        self.client_id = client_id
        self._generate_key()
        self.codes: dict[str, dict] = {}
        self.user = {"sub": "u-ada", "name": "Ada Lovelace", "email": "ada@corp.test", "groups": ["data"]}
        self.exp_delta = 600

    def _generate_key(self) -> None:
        private_pem = rsa.generate_private_key(65537, 2048).private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        self.key = RSAKey.import_key(private_pem)

    def rotate_key(self) -> None:
        self._generate_key()

    def app(self) -> Starlette:
        async def discovery(request):
            return JSONResponse(
                {
                    "issuer": self.issuer,
                    "authorization_endpoint": f"{self.issuer}/protocol/openid-connect/auth",
                    "token_endpoint": f"{self.issuer}/protocol/openid-connect/token",
                    "jwks_uri": f"{self.issuer}/protocol/openid-connect/certs",
                }
            )

        async def authorize(request):
            q = request.query_params
            code = "code-" + q.get("state", "")[:8]
            self.codes[code] = {"nonce": q.get("nonce", "")}
            location = q["redirect_uri"] + "?" + urlencode({"code": code, "state": q.get("state", "")})
            return RedirectResponse(location)

        async def token(request):
            form = parse_qs((await request.body()).decode())
            code = form.get("code", [""])[0]
            info = self.codes.get(code, {"nonce": ""})
            now = int(time.time())
            claims = {
                "iss": self.issuer,
                "sub": self.user["sub"],
                "aud": self.client_id,
                "exp": now + self.exp_delta,
                "iat": now,
                "nonce": info["nonce"],
                "name": self.user["name"],
                "email": self.user["email"],
                "groups": self.user["groups"],
            }
            id_token = jose_jwt.encode({"alg": "RS256", "kid": "test", "typ": "JWT"}, claims, self.key)
            return JSONResponse({"access_token": "at", "id_token": id_token, "token_type": "Bearer"})

        async def jwks(request):
            public = self.key.as_dict(is_private=False)
            public.update({"kid": "test", "use": "sig", "alg": "RS256"})
            return JSONResponse({"keys": [public]})

        base = urlparse(self.issuer).path
        return Starlette(
            routes=[
                Route(f"{base}/.well-known/openid-configuration", discovery),
                Route(f"{base}/protocol/openid-connect/auth", authorize),
                Route(f"{base}/protocol/openid-connect/token", token, methods=["POST"]),
                Route(f"{base}/protocol/openid-connect/certs", jwks),
            ]
        )
