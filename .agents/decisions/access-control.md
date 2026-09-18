# Decisions: access control

| # | Where it lives |
|---|---|
| D7 | `src/dct_hub/auth/access.py` |
| D8 | `authorize(...)` calls in `src/dct_hub/api.py`, `src/dct_hub/ui.py` |
| D9 | `src/dct_hub/auth/oidc.py` |

### D7. Most-specific path shadows; same-pattern grants union
`restricted/*` overrides `*` for its subtree (no fall-through — carve-outs
must be possible), while multiple grants on the identical pattern combine.
**Why:** fall-through semantics made `restricted/*` useless; first-match-wins
made `*` viewer + `*` operator-by-group unwritable.
**Rejected:** longest-prefix with identity fall-through; first-match-wins.

### D8. `refresh` capability gates every warehouse query
Including render-on-miss page views and the UI refresh button. Viewers can
only ever see already-rendered artifacts (plus system-driven stale-while-
revalidate, bounded by TTL + `min_interval_s`).
**Why:** an unauthenticated/unprivileged "view" must never cost warehouse work.

### D9. Hand-rolled OIDC (auth-code + PKCE) with joserfc; no OAuth framework
Lazy discovery (hub boots while IdP is down), state/nonce/PKCE in the signed
session cookie, RS256 validation against JWKS, one JWKS refetch on validation
failure (survives Keycloak key rotation), all token errors → clean 401.
**Why:** air-gapped reviewability; authlib's `authlib.jose` is deprecated in
favor of `joserfc`, so we depend on `joserfc` directly.
**Conventions:** `azp` unchecked (single-aud Keycloak default); session is
signed-not-encrypted with cookie `max_age` only (no server-side revocation).
