# Deployment guide (air-gapped)

The image builds and runs with **no index/registry access at install time**:
all Python packages come from wheels vendored ahead of time. For how this
fits into the wider CI/CD chain (board deploys, warming, upgrades), see
[operations.md](operations.md).

## 1. Vendor (on a networked machine)

```bash
scripts/vendor_wheels.sh
```

This produces:

- `requirements.lock` — full dependency closure of dct-hub + `dbt-charts`
  (pinned in `requirements-dbt.txt`), with PyPI artifact hashes. **Commit this.**
- `image.lock` — hashes of the actual vendored wheels. **Commit this.** The
  image install verifies every third-party wheel against it
  (`pip --require-hashes`); it exists because wheels rebuilt from sdists
  never match their sdist's hash in `requirements.lock`.
- `wheels/` — manylinux x86_64 wheels for the lock. Sdist-only packages
  (e.g. `dbt-core-experimental-parser`, `titlecase`) are pre-built into wheels,
  because the offline install can't compile from source. Native sdists build
  for the host platform: vendor on linux/amd64 (or inside the base image) for
  x86_64 builds. Not committed.
- `dist/` — the dct-hub wheel. Not committed.
- `image-requirements.txt` — the dbt-charts pin extracted from the lock
  (quick human-readable reference; the image install uses `image.lock`).
  Committed.

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

## 4. HA topology (active-active)

Replicas are stateless: run **two or more** behind a load balancer that
health-checks `/healthz`. Shared state lives outside the containers:

```
LB ──► dct-hub replica A ─┐
   ──► dct-hub replica B ─┼──► PostgreSQL   (metadata, job queue, rate limit,
   ──► dct-hub replica C ─┘    advisory-lock leadership)
                          └──► S3 / MinIO   (artifact blobs)
```

```yaml
# charts-tool.yml (same file on every replica)
storage:
  dir: /state                       # staging scratch only, per replica
  postgres: ${DCT_HUB_PG_DSN}       # postgresql://user:pass@pg-host/db
  s3:
    bucket: dct-hub-artifacts
    endpoint_url: http://minio.internal:9000   # omit for AWS
auth:
  session_secret: ${DCT_HUB_SESSION_SECRET}    # MUST be identical on all
                                               # replicas (signed cookies)
```

- Any supported PostgreSQL works (claims use `FOR UPDATE SKIP LOCKED`). One
  small database is plenty — write volume is render-queue traffic.
- S3-compatible object storage; MinIO for air-gapped. Credentials via the
  standard env chain on each replica.
- **Single-flight, the render queue, and the rate limit are cluster-wide** —
  a board renders once no matter how many replicas are asked. `/api/warm`
  needs no per-replica fan-out: one call warms the shared store.
- Housekeeping (stale-job reaper, rate-hit pruning, retention sweep) runs on
  a leader elected by advisory lock; if the leader dies, another replica
  takes over within a minute.
- The `ha` extra (`asyncpg`, `aiobotocore`) is baked into the image; for bare
  installs use `pip install 'dct-hub[ha]'`.
- PG-backed integration tests run in CI with a Postgres service by exporting
  `DCT_HUB_TEST_PG_DSN` (locally they skip).
- Switching modes starts a fresh store — re-warm (`POST /api/warm`) after
  cutover.

## 5. Production checklist

- [ ] `auth.enabled: true` with a real `session_secret` from a secret manager
      (identical on every replica when running HA — sessions are signed cookies)
- [ ] `session_https_only: true` behind TLS (startup warns when auth is on
      without it); HSTS belongs to the TLS-terminating ingress, not the app
- [ ] Service tokens via `${ENV_VAR}` (CI/cron), groups mapped to an operator role
- [ ] HA: `storage.postgres` + `storage.s3` set; ≥2 replicas behind an LB that
      health-checks `/healthz`; warehouse sized for `max_concurrent` × replicas
- [ ] Grants reviewed: `*` viewer is convenient but shadow carve-outs
      (`restricted/*`) for sensitive folders
- [ ] `render.query_cache` set for warm re-renders (forces render concurrency to 1 — expected)
- [ ] `policy.default_ttl_s` / `min_interval_s` tuned to warehouse cost tolerance
- [ ] `policy.max_renders_per_minute` reviewed: default 30/identity/min covers
      interactive use; raise it for long `warm.boards` lists or busy
      service-token identities, `0` disables the cap
- [ ] Audit logging shipped: `dct_hub.auth` (logins, logouts, token rejections)
      and `dct_hub.access` (grant denials) log to stderr alongside uvicorn
- [ ] `retention.enabled: true` once comfortable — frozen snapshots are exempt,
      and unclassifiable artifacts are never pruned
- [ ] Warehouse credentials via env/secrets, never in `charts-tool.yml`
- [ ] `warm.boards` lists the top boards; the deploy pipeline calls
      `curl -f -X POST /api/warm` after each sync (and after dct upgrades) so
      no viewer pays a cold miss. Intraday boards that must stay fresh between
      views still want a schedule:
      `POST /api/renders {"board": "...", "vars": {"day": "<today>"}, "force": true, "wait": true}`

## Known limitation

The first real `docker build` has not been run on the dev machine (no Docker
daemon); the offline-install mechanism was verified with an equivalent
`--no-index --require-hashes` install from the vendored wheels against
`image.lock` (positive and tamper cases). Note the vendored `wheels/` set must
be rebuilt on linux/amd64 before the first x86_64 image build — it currently
contains a macOS-built wheel for the sdist-only `dbt-core-experimental-parser`.
Run the first build in CI before rollout.
