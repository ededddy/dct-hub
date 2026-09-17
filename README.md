# dct-hub

Serving layer for [dbt Charts](https://docs.dbtcharts.com/) (`dct`): on-demand
board rendering over HTTP, a content-addressed artifact cache (frozen snapshots
for past dates, TTL for today), access control, and a catalog UI.

Status: **M1** — config loader, `dct` CLI wrapper, artifact store with cache
keys, and a synchronous render API. Auth (Keycloak OIDC), the async render
queue with freeze/TTL policy, and the catalog UI come in later milestones.

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

## Tests

```bash
pytest            # unit tests + an e2e test (skipped when dct is absent)
```
