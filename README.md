# dct-hub

Serving layer for [dbt Charts](https://docs.dbtcharts.com/) (`dct`): on-demand
board rendering over HTTP, a content-addressed artifact cache (frozen snapshots
for past dates, TTL for today), access control, and a catalog UI.

Status: **M3** — M1 (render API, artifact store, cache keys) + M2 (Keycloak
OIDC, service tokens, path-based grants) + async render queue with
single-flight coalescing, and the freshness policy: frozen snapshots for past
dates, TTL for current data, stale-while-revalidate on views, per-artifact
rate limiting. The catalog UI comes in M4.

## Quickstart

Requires the `dct` CLI (`uv tool install dbt-charts`).

```bash
uv venv && source .venv/bin/activate
uv pip install -e ".[dev]"
dct-hub --config charts-tool.yml        # serves http://127.0.0.1:8080
```

The repo ships a sample dbt Charts project in `sample_project/` (CSV source,
no warehouse needed). Regenerate its data — with a fuller "today" to simulate
an intraday report filling in — via:

```bash
python sample_project/scripts/gen_data.py [--complete-today]
```

## API

```bash
# Trigger a render. Async by default: 202 + job id. `wait: true` blocks.
curl -X POST localhost:8080/api/renders \
  -H 'content-type: application/json' \
  -d '{"board": "sales_daily", "vars": {"day": "2026-09-16"}, "wait": true}'
# outcome: cached | queued | running | done — "cached" means the existing
# artifact was served (frozen or within TTL); "stale: true" marks a
# rate-limited stale serve. force: true always re-renders.

curl localhost:8080/api/jobs/<job_id>          # job status (queued/running/done/error)
curl localhost:8080/api/jobs                   # recent jobs (view-filtered)
curl localhost:8080/api/renders/<key>          # artifact metadata
curl localhost:8080/api/renders/<key>/artifact # raw artifact
curl localhost:8080/api/boards                 # catalog
curl localhost:8080/api/boards/sales_daily     # dct describe passthrough
open  localhost:8080/b/sales_daily?day=2026-09-16  # board HTML
```

Artifacts land in `.hub/artifacts/<board>/<key>.html`, render and job metadata
in `.hub/meta.db`. A key covers board path, variable values, board source
(including the `meta.yml` chain and `dbt_charts.yml`), format and dct version —
editing a board or upgrading dct produces a fresh render automatically.

## Freshness policy (`policy:` in charts-tool.yml)

- **Frozen**: a board whose date-picker variables are all in the past is
  rendered once and served forever — yesterday's snapshot is immutable.
- **Fresh/stale**: anything else is served from cache for `default_ttl_s`;
  past that, views serve the stale artifact immediately while a background
  refresh is enqueued (stale-while-revalidate), and `POST /api/renders`
  returns 202.
- **Rate limit**: re-renders of the same artifact are blocked within
  `min_interval_s` of the last job (`force: true` excepted).
- Identical concurrent render requests coalesce onto one job (single-flight).
  With `render.query_cache` set, renders serialize (DuckDB single-writer).

## Access control

Disabled by default for the dev loop. With `auth.enabled: true` (see the
annotated example in `charts-tool.yml`):

- **Humans** log in via Keycloak OIDC (authorization code + PKCE, session
  cookie). Unauthenticated browser requests to `/b/...` redirect to SSO;
  API requests get `401`.
- **Machines** (CI, cron) use `Authorization: Bearer <token>` with service
  tokens from config; each token carries a name and group list. `${ENV_VAR}`
  references are expanded for secrets.
- **Authorization** is path-based: `access.grants` maps board path patterns
  (`*`, `finance/*`, `exec/board`) to roles for groups/users. The most
  specific path pattern wins and shadows broader ones (so `restricted/*` can
  carve a subtree out of a `*` grant); grants on the same pattern union.
  Capabilities: `view` (boards, artifacts, catalog) and `refresh` (any
  warehouse-querying render, including render-on-miss views). Unknown callers
  are denied; the catalog only lists boards the caller can view.

`/auth/me` shows the current identity; `/auth/logout` clears the session.
When auth is enabled the OpenAPI docs endpoints are not exposed.

## Tests

```bash
pytest            # unit tests + an e2e test (skipped when dct is absent)
```
