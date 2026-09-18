# Deployment guide (air-gapped)

The image builds and runs with **no index/registry access at install time**:
all Python packages come from wheels vendored ahead of time.

## 1. Vendor (on a networked machine)

```bash
scripts/vendor_wheels.sh
```

This produces:

- `requirements.lock` — full dependency closure of dct-hub + `dbt-charts`
  (pinned in `requirements-dbt.txt`), with hashes. **Commit this.**
- `wheels/` — manylinux x86_64 wheels for the lock. Sdist-only packages
  (e.g. `dbt-core-experimental-parser`, `titlecase`) are pre-built into wheels,
  because the offline install can't compile from source. Not committed.
- `dist/` — the dct-hub wheel. Not committed.
- `image-requirements.txt` — the dbt-charts pin extracted from the lock
  (single source of truth). Committed.

Hashes are verified at **vendor time** (`pip download` checks every file
against the lock). The image install therefore resolves pinned versions from
the verified set without re-checking hashes — which also sidesteps the fact
that locally-built wheels never match their sdist's hash.

ARM targets: `PLATFORM=aarch64-unknown-linux-gnu PLATFORM_ARCH=aarch64 scripts/vendor_wheels.sh`.

## 2. Build & run

```bash
docker build -t dct-hub .
docker run -p 8080:8080 \
  -v $PWD/charts-tool.yml:/config/charts-tool.yml:ro \
  -v $PWD/my-dct-project:/project \
  -v dct-hub-data:/state \
  dct-hub
```

Container contract:

| Mount | Purpose |
|---|---|
| `/config/charts-tool.yml` (ro) | Hub config; config path from `DCT_HUB_CONFIG` |
| `/project` | The dct project (`project_dir: /project`) |
| `/state` (volume) | `storage.dir: /state` — artifacts, meta.db, query cache |

The image runs as non-root (`dcthub`, uid 10001) with a writable `/state`,
and health-checks `GET /healthz` via stdlib urllib (no curl in slim).

## 3. Keycloak setup

1. Create a client (e.g. `dct-hub`) in your realm: confidential, standard
   flow, PKCE (S256 is used regardless).
2. Valid redirect URI: `https://<hub-host>/auth/callback`.
3. Add a **groups mapper** (or realm-roles mapper) so group membership lands
   in the ID token; set `auth.oidc.groups_claim` to the claim name
   (e.g. `groups`). Keycloak often emits slash-prefixed paths like
   `/data/finance` — grants must match those strings exactly.
4. Config: `issuer: https://keycloak.corp.internal/realms/<realm>`,
   `client_id`, `client_secret: ${KEYCLOAK_CLIENT_SECRET}`.
5. Put the hub's CA in the system trust store of the image if your Keycloak
   uses an internal CA (extend the Dockerfile or mount and set
   `SSL_CERT_FILE`).

## 4. Production checklist

- [ ] `auth.enabled: true` with a real `session_secret` from a secret manager
- [ ] `session_https_only: true` behind TLS
- [ ] Service tokens via `${ENV_VAR}` (CI/cron), groups mapped to an operator role
- [ ] Grants reviewed: `*` viewer is convenient but shadow carve-outs
      (`restricted/*`) for sensitive folders
- [ ] `render.query_cache` set for warm re-renders (forces render concurrency to 1 — expected)
- [ ] `policy.default_ttl_s` / `min_interval_s` tuned to warehouse cost tolerance
- [ ] `policy.max_renders_per_minute` set as an abuse cap on renders (auto and
      force) — leave headroom for the cron/service-token identity's bursts
- [ ] Audit logging shipped: `dct_hub.auth` (logins, logouts, token rejections)
      and `dct_hub.access` (grant denials) log to stderr alongside uvicorn
- [ ] `retention.enabled: true` once comfortable — frozen snapshots are exempt,
      and unclassifiable artifacts are never pruned
- [ ] Warehouse credentials via env/secrets, never in `charts-tool.yml`
- [ ] Cron/Airflow hooks for intraday boards:
      `POST /api/renders {"board": "...", "vars": {"day": "<today>"}, "force": true, "wait": true}`

## Known limitation

The first real `docker build` has not been run on the dev machine (no Docker
daemon); the offline-install mechanism was verified with an equivalent
`--no-index` install from the vendored wheels. Run the first build in CI
before rollout.
