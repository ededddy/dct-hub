"""Auth and access-control configuration (the `auth:` and `access:` sections).

Values of the form `${ENV_VAR}` in secret fields are expanded from the
environment; a missing variable expands to empty (which simply never matches).
"""

import os

from pydantic import BaseModel, ConfigDict, Field, model_validator


def expand_env(value: str) -> str:
    if value.startswith("${") and value.endswith("}"):
        return os.environ.get(value[2:-1], "")
    return value


class OidcConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    issuer: str
    client_id: str
    client_secret: str = ""
    scopes: str = "openid profile email"
    groups_claim: str = "groups"
    # Absolute callback URL as registered with the IdP. When unset, the
    # callback URL is derived from the incoming request's scheme/host — pin it
    # when the hub sits behind a proxy or Host-header-derived URLs are unwanted.
    redirect_url: str | None = None

    @model_validator(mode="after")
    def _expand(self) -> "OidcConfig":
        self.client_secret = expand_env(self.client_secret)
        return self


class ServiceTokenConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str
    name: str
    groups: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _expand(self) -> "ServiceTokenConfig":
        self.token = expand_env(self.token)
        return self


class AuthConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    session_secret: str = "dct-hub-dev-secret"
    session_https_only: bool = False
    session_max_age_s: int = 12 * 3600
    oidc: OidcConfig | None = None
    service_tokens: list[ServiceTokenConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def _expand_and_check(self) -> "AuthConfig":
        self.session_secret = expand_env(self.session_secret)
        if self.enabled and self.oidc is None and not self.service_tokens:
            raise ValueError("auth.enabled requires oidc and/or service_tokens")
        if self.enabled and self.session_secret == "dct-hub-dev-secret":
            raise ValueError("auth.enabled requires a real session_secret")
        return self


class Grant(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    role: str
    groups: list[str] = Field(default_factory=list)
    users: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> "Grant":
        if not self.groups and not self.users:
            raise ValueError(f"grant on {self.path!r} names neither groups nor users")
        return self


class AccessConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    roles: dict[str, list[str]] = Field(
        default_factory=lambda: {
            "viewer": ["view"],
            "operator": ["view", "refresh"],
            "admin": ["view", "refresh", "manage"],
        }
    )
    grants: list[Grant] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> "AccessConfig":
        unknown = {g.role for g in self.grants} - set(self.roles)
        if unknown:
            raise ValueError(f"grants reference unknown roles: {sorted(unknown)}")
        return self
