# dct-hub

Serving layer for [dbt Charts](https://docs.dbtcharts.com/) (`dct`): on-demand
board rendering over HTTP, a content-addressed artifact cache (frozen snapshots
for past dates, TTL for today), access control, and a catalog UI.

Status: **M5** — full feature set: render API + artifact cache (M1), access
control (M2), render queue + freshness policy (M3), catalog UI (M4), and
air-gapped packaging + retention (M5).

## Deployment (air-gapped)

For the end-to-end operating model — team responsibilities, CI/CD chaining,
deploy-time warming, upgrades, and failure modes — see
[docs/operations.md](docs/operations.md). This section covers the image build.

On a networked machine, vendor the dependencies (locked with hashes; sdists
pre-built to wheels), then build the image — no index access needed:

```bash
scripts/vendor_wheels.sh        # -> requirements.lock, wheels/, dist/
docker build -t dct-hub .
```

Run it with your project and config mounted (state persists in a volume):

```bash
docker run -p 8080:8080 \
  -v $PWD/charts-tool.yml:/config/charts-tool.yml:ro \
  -v $PWD/my-dct-project:/project \
  -v dct-hub-data:/state \
  dct-hub
```

In `charts-tool.yml`, point `project_dir` at `/project` and `storage.dir` at
`/state`. Warehouse credentials go through environment variables (`-e` /
secrets), never in the config. ARM builds: `PLATFORM=aarch64-unknown-linux-gnu
PLATFORM_ARCH=aarch64 scripts/vendor_wheels.sh`.

**HA (active-active):** set `storage.postgres` + `storage.s3` and run ≥2
replicas behind a load balancer — they are stateless. Single-flight, the
render queue, and the rate limit become cluster-wide, artifacts stream from
S3, and housekeeping is leader-elected via advisory lock. The image bakes in
the `ha` extra (`asyncpg`, `aiobotocore`). See `docs/deployment.md` § HA
topology.

Retention is off by default; enable the sweeper in config to prune non-frozen
artifacts by age (`max_age_days`) and count (`max_per_board`). Frozen
snapshots are never pruned, and an unclassifiable artifact (e.g. `dct
describe` failing mid-sweep) is always kept — pruning fails safe.

## UI

`/` is the catalog home: search (via `dct search`, grant-filtered), boards
grouped by folder, freshness badges, favorites and recently-viewed
(client-side, localStorage). `/b/<board>` is the board page: auto-generated
variable controls (select/date/number/text from the board's declared
variables), freshness badge (frozen snapshot / fresh / stale), a refresh
button shown only with the `refresh` grant, and snapshot history with links to
earlier variable combinations. The rendered artifact loads in an iframe from
`/raw/<board>` (the M1–M3 artifact endpoint moved there; `/b/` is now the
shell). The shell follows the OS light/dark theme with a manual override in
the header, and the board page has an Auto/Light/Dark backdrop switcher behind
the iframe so differently-themed artifacts sit well; both choices persist
client-side. All assets are inline — no CDN, air-gap safe.

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

# Deploy-pipeline warm step: render the boards listed under `warm:` in
# charts-tool.yml (unchanged boards return "cached"; any failure → 502).
curl -X POST localhost:8080/api/warm -H 'content-type: application/json' -d '{"wait": true}'

curl localhost:8080/api/jobs/<job_id>          # job status (queued/running/done/error)
curl localhost:8080/api/jobs                   # recent jobs (view-filtered)
curl localhost:8080/api/renders/<key>          # artifact metadata
curl localhost:8080/api/renders/<key>/artifact # raw artifact
curl localhost:8080/api/boards                 # catalog
curl localhost:8080/api/boards/sales_daily     # dct describe passthrough
open  localhost:8080/                              # catalog home (UI)
open  localhost:8080/b/sales_daily?day=2026-09-16  # board page (shell + controls)
curl "localhost:8080/raw/sales_daily?day=2026-09-16"  # raw artifact HTML
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
