"""Path-based authorization.

Grant paths use board addressing (no leading slash): `*` matches everything,
`finance` matches exactly that board, `finance/*` matches every board under the
finance folder. The most specific path pattern wins and less specific patterns
do NOT fall through — so a broad `*` viewer grant can be carved back for a
restricted subtree (`restricted/*` granted to a smaller group). Grants sharing
the winning pattern are unioned, so one pattern can give different roles to
different groups. No matching grant means deny.
"""

from fastapi import HTTPException, status

from .config import AccessConfig
from .identity import Identity


class Unauthenticated(HTTPException):
    def __init__(self, detail: str = "authentication required"):
        super().__init__(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)


class Forbidden(HTTPException):
    def __init__(self, detail: str = "insufficient grant"):
        super().__init__(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


def _specificity(pattern: str) -> tuple[int, int]:
    if pattern == "*":
        return (0, 0)
    if pattern.endswith("/*"):
        return (1, len(pattern))
    return (2, len(pattern))


def _matches(pattern: str, board: str) -> bool:
    if pattern == "*":
        return True
    if pattern.endswith("/*"):
        return board.startswith(pattern[:-1])
    return board == pattern


class AccessPolicy:
    def __init__(self, config: AccessConfig):
        self.roles = {name: set(caps) for name, caps in config.roles.items()}
        self.grants = sorted(
            config.grants,
            key=lambda g: _specificity(g.path.lstrip("/")),
            reverse=True,
        )

    def _grant_matches_identity(self, grant, identity: Identity) -> bool:
        if set(grant.groups) & set(identity.groups):
            return True
        return identity.sub in grant.users or identity.email in grant.users

    def capabilities(self, identity: Identity, board: str) -> set[str]:
        best_pattern = None
        caps: set[str] = set()
        for grant in self.grants:  # sorted most specific first
            path = grant.path.lstrip("/")
            if not _matches(path, board):
                continue
            if best_pattern is None:
                best_pattern = path
            elif path != best_pattern:
                break  # less specific patterns are shadowed
            if self._grant_matches_identity(grant, identity):
                caps |= self.roles.get(grant.role, set())
        return caps

    def allows(self, identity: Identity, capability: str, board: str) -> bool:
        return capability in self.capabilities(identity, board)


def require(policy: AccessPolicy | None, identity: Identity | None, capability: str, board: str) -> None:
    if policy is None:
        return
    if identity is None:
        raise Unauthenticated()
    if not policy.allows(identity, capability, board):
        raise Forbidden(f"{identity.sub} lacks '{capability}' on {board!r}")
