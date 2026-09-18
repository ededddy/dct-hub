# dct-hub architecture

dct-hub is a serving layer around [dbt Charts](https://docs.dbtcharts.com/) (`dct`).
It solves three problems with the stock workflow:

1. **CI-time builds** — boards rendered in CI go stale; refreshing means re-running CI.
   dct-hub renders on demand over HTTP, with variables and a snapshot cache.
2. **No access control in `dct serve`** — dct-hub adds Keycloak OIDC SSO, service
   tokens, and path-based grants.
3. **Weak navigation UI** — dct-hub adds a catalog home and board pages with
   variable controls, snapshot history, and freshness badges.

## Components

```
browser / CI ──► FastAPI app (api.py)
                   ├─ auth/        OIDC flow (oidc.py), Bearer tokens (identity.py),
                   │               path grants (access.py), config (config.py)
                   ├─ ui.py        Jinja catalog + board shell (templates/)
                   ├─ policy.py    freshness: FROZEN / FRESH / STALE
                   ├─ queue.py     async render queue, single-flight, worker pool
                   ├─ store.py     metadata interface + LocalStore (SQLite, single-node)
                   ├─ store_pg.py  PgStore (Postgres, HA) + cluster rate limiter
                   ├─ blobs.py     artifact blobs: local FS (atomic) or S3 (HA)
                   ├─ gc.py        retention sweeper; HA janitor (leader-elected)
                   └─ dct.py       async wrapper over the dct CLI (subprocess)
```

The hub never implements rendering itself. Every render is
`dct render <board> --var k=v --format html --output <path>` in a subprocess,
which keeps dct version-locked in `requirements-dbt.txt` and the hub free of
rendering internals.

## Request flows

**Trigger a render (async):**
`POST /api/renders` → authorize `refresh` → compute cache key → if a usable
artifact is FROZEN/FRESH, return `cached` → else submit a job (202 + `job_id`;
`wait: true` blocks). Identical in-flight requests coalesce onto the same job
(single-flight).

**View a board:**
`GET /b/<board>` returns a shell page instantly (controls, badges, history).
The shell never renders. Its iframe loads `GET /raw/<board>`, which is where
render-on-miss (requires `refresh` grant) and stale-while-revalidate live:
a STALE artifact is served immediately and a background refresh is enqueued,
bounded by `min_interval_s`.

**Refresh button (UI):** POST force render → poll `GET /api/jobs/{id}` →
reload iframe on `done`. The button exists only for identities with the
`refresh` grant, and the POST enforces it again server-side.

## Cache model

Two layers:

- **L1 — dct query-result cache** (DuckDB file, `render.query_cache`): makes
  re-renders cheap when queries haven't changed. Single-writer → when enabled,
  the render worker pool is forced to 1.
- **L2 — artifact store** (blob + metadata): the hub's content-addressed
  cache. The key hashes: board path, sorted variables, board source
  fingerprint (board file + `meta.yml` chain + `dbt_charts.yml`), output
  format, and dct version. Editing a board or upgrading dct invalidates
  automatically. Blobs live on local disk (`artifacts/<board>/<key>.<fmt>`)
  or in S3 in HA mode; the renders row stores the blob locator. Publishing is
  blob-then-row, deleting row-then-blob — a row always implies its blob.

Freshness state machine per artifact (policy.py):

- **FROZEN** — every declared `date`-type variable (from `dct describe`,
  defaults included) is in the past → immutable, TTL does not apply.
  Yesterday's snapshot is rendered once and served forever.
- **FRESH** — younger than `policy.default_ttl_s` → served.
- **STALE** — older → served while a background refresh runs
  (rate-limited by `min_interval_s`; `force: true` bypasses).
- describe unavailable → degrade to TTL (serving never depends on policy
  metadata); the retention sweeper uses a stricter tri-state (`policy.frozen()`)
  so it never prunes what it can't classify.

## Data model

Single-node: `.hub/meta.db` (SQLite). HA mode: the same two tables in
Postgres, plus `rate_hits` (D18).

- `renders` — one row per artifact key: board, vars, format, blob locator,
  status, duration, rendered_at, dct_version. **Only successful renders**:
  a failed refresh must never displace the last good artifact.
- `jobs` — execution audit: id, key, board, vars, mode (auto/force), status
  (queued/running/done/error/interrupted), error tail, requested_by, timings,
  claimed_by (which replica ran it). Errors live here, not in `renders`.

## Concurrency model

**Single-node (default):** one process, asyncio throughout. Worker pool size
`policy.max_concurrent` (forced to 1 when the DuckDB query cache is set).
Submit/claim do their check-then-insert without awaits in between, so
single-flight is race-free on one loop.

**HA mode (`storage.postgres` set):** N stateless replicas behind a load
balancer. Single-flight is enforced by a partial unique index on active jobs
per key; workers claim with `FOR UPDATE SKIP LOCKED` and stamp `claimed_by`;
`wait` combines a local event with a 0.5s poll so callers observe jobs
finished by other replicas; the rate limiter is a shared hits table; a
janitor (advisory-lock leader, any one replica) reaps jobs orphaned by dead
replicas, prunes rate hits, and runs the retention sweep. OIDC sessions are
signed cookies, so login flows cross replicas freely as long as every
replica shares `session_secret`. The L1 query cache and the describe cache
stay per-replica (self-healing). See D18 for the invariants.

## UI architecture

Server-rendered Jinja templates, zero external assets (air-gapped). The board
page is a shell + `<iframe src="/raw/<board>">` so the page paints instantly
while a first render runs; a one-shot guarded reload syncs the freshness badge
afterwards. Favorites/recents live in localStorage — no per-user server state.
`/raw/` responses carry `Cache-Control: no-cache` so refreshes show through.

See `.agents/decisions/` for the rationale behind the load-bearing choices.
