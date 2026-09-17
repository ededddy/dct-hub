"""Who is calling: an Identity resolved from a Bearer token or the session cookie."""

import hmac
from dataclasses import dataclass, field

from starlette.requests import Request

from .config import AuthConfig


@dataclass
class Identity:
    sub: str
    name: str
    email: str
    groups: frozenset[str] = field(default_factory=frozenset)
    via: str = "anon"  # "oidc" | "token" | "anon"


def resolve_identity(request: Request, auth: AuthConfig) -> Identity | None:
    if not auth.enabled:
        return Identity(sub="anon", name="anonymous", email="", via="anon")

    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        token = header.split(None, 1)[1].strip()
        for entry in auth.service_tokens:
            if entry.token and hmac.compare_digest(token, entry.token):
                return Identity(sub=entry.name, name=entry.name, email="", groups=frozenset(entry.groups), via="token")
        return None

    # Reached only when auth is enabled, which always installs SessionMiddleware.
    user = request.session.get("user")
    if user:
        return Identity(
            sub=user.get("sub", ""),
            name=user.get("name", ""),
            email=user.get("email", ""),
            groups=frozenset(user.get("groups", [])),
            via="oidc",
        )
    return None
