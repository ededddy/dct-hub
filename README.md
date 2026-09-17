# dct-hub

Serving layer for [dbt Charts](https://docs.dbtcharts.com/) (`dct`): on-demand
board rendering over HTTP, a content-addressed artifact cache (frozen snapshots
for past dates, TTL for today), access control, and a catalog UI.

Status: **M2** — M1 (config loader, `dct` CLI wrapper, artifact store with
cache keys, synchronous render API) plus access control: Keycloak OIDC login
for browsers, Bearer service tokens for CI, and path-based grants. The async
render queue with freeze/TTL policy and the catalog UI come in later milestones.

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
# Trigger a render (cached if an artifact for board+vars already exists)
curl -X POST localhost:8080/api/renders \
  -H 'content-type: application/json' \
  -d '{"board": "sales_daily", "vars": {"day": "2026-09-16", "region": "West"}}'

curl localhost:8080/api/renders/<key>            # render metadata
curl localhost:8080/api/renders/<key>/artifact   # raw artifact
curl localhost:8080/api/boards                   # catalog
curl localhost:8080/api/boards/sales_daily       # dct describe passthrough
open  localhost:8080/b/sales_daily?day=2026-09-16  # board HTML (renders on miss)
```

Artifacts land in `.hub/artifacts/<board>/<key>.html`, metadata in
`.hub/meta.db`. A key covers board path, variable values, board source
(including the `meta.yml` chain), format and dct version — editing a board or
upgrading dct produces a fresh render automatically.

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
