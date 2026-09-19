# Configuration reference

All configuration lives in `charts-tool.yml` (path given by `--config`, or
`DCT_HUB_CONFIG` in the container). Relative paths resolve against the config
file's directory. Unknown keys are rejected (`extra="forbid"`) — a typo fails
at startup, not silently at runtime. Secret-looking fields accept
`${ENV_VAR}` references, expanded at load.

## Top level

| Key | Default | Purpose |
|---|---|---|
| `project_dir` | (required) | dct project root (contains `dbt_charts.yml`, `charts/`) |
| `host` / `port` | `127.0.0.1` / `8080` | Bind address |
| `dct_bin` | `dct` | dct CLI executable |

## `storage:`

| Key | Default | Purpose |
|---|---|---|
| `dir` | `.hub` | Local state: artifacts (`artifacts/`) + metadata DB (`meta.db`). In HA mode: staging scratch only |
| `postgres` | unset | Postgres DSN, `${ENV_VAR}` expanded. **Set = HA mode**: metadata, queue, rate limit, leadership in Postgres; artifacts in S3; N replicas share them |
| `s3` | unset | **Required when `postgres` is set.** `bucket`; `endpoint_url` (MinIO/on-prem — omit for AWS); `prefix` (default `artifacts/`); `region`. Credentials via the standard env chain (`AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`), never this file |

Rules: `postgres` without `s3.bucket` is rejected, as is `s3` without
`postgres` (shared blobs need shared metadata). HA mode requires the `ha`
extra (`asyncpg`, `aiobotocore` — included in the Docker image). Switching
modes starts a fresh store: no migration — re-warm after switching.

## `render:`

| Key | Default | Purpose |
|---|---|---|
| `format` | `html` | Default render format |
| `timeout_s` | `120` | Per-render timeout |
| `query_cache` | unset | Path to a shared dct DuckDB query cache (L1). When set, render concurrency is forced to 1 (DuckDB single-writer). |

## `policy:` — freshness

| Key | Default | Purpose |
|---|---|---|
| `default_ttl_s` | `3600` | Artifacts younger than this are FRESH |
| `frozen_date_vars` | `true` | All-past date variables → immutable artifact |
| `min_interval_s` | `300` | Min seconds between renders of one artifact (`force` excepted) |
| `max_concurrent` | `2` | Render workers (forced to 1 if `render.query_cache` set) |
| `max_renders_per_minute` | `30` | Per-identity cap on render submissions per minute, across auto and force. `0` = unlimited |

Variables reaching a render are validated against the board's `dct describe`
declarations before any warehouse query runs: unknown names, out-of-list
option values, and ill-typed date/number/checkbox values are rejected with
`400`; if `describe` is unavailable the render is refused with `502` (fail
closed). Serving an already-rendered artifact never validates. Free-text
variables get no value check — board authors must quote them in SQL with
dct's quoting helper, never interpolate them raw.

## `retention:` — artifact GC (opt-in)

| Key | Default | Purpose |
|---|---|---|
| `enabled` | `false` | Master switch |
| `max_age_days` | `30` | Prune non-frozen artifacts older than this |
| `max_per_board` | `100` | Keep newest N non-frozen artifacts per board |
| `sweep_interval_s` | `3600` | Sweep cadence |

Frozen snapshots are never pruned. Artifacts that can't be classified (e.g.
`dct describe` failing) are always kept — pruning fails safe. Renders whose
board was deleted from the repo age out normally.

## `warm:` — deploy-time cache warming

Boards rendered by `POST /api/warm` (the deploy-pipeline warm step; see
`docs/api.md`). Auto semantics: changed boards render, unchanged boards
report `cached` — no `force`, so warming costs nothing on no-op deploys.

```yaml
warm:
  boards:
    - sales_daily                 # warmed with declared variable defaults
    - board: exec/overview        # mapping form: extra explicit combos
      vars:
        - {region: East}
        - {region: West}
```

| Key | Default | Purpose |
|---|---|---|
| `boards` | `[]` | Board refs (string form) or `{board, vars}` entries |
| `vars` | `[{}]` | Variable combos to warm per board; `{}` = the board's declared defaults |

Warming runs warehouse queries: the calling identity needs `refresh` on each
board, and submissions count against `policy.max_renders_per_minute` — size
the cap with the warm list in mind.

## `auth:`

| Key | Default | Purpose |
|---|---|---|
| `enabled` | `false` | Master switch. Off = anonymous full access (dev loop) |
| `session_secret` | — | Cookie signing key, `${ENV_VAR}`. Required when enabled; the dev default is rejected |
| `session_https_only` | `false` | Set `true` behind TLS (production); startup warns when auth is on without it |
| `session_max_age_s` | `43200` | Session cookie lifetime |
| `oidc` | unset | Keycloak: `issuer` (realm URL), `client_id`, `client_secret`, `scopes`, `groups_claim`, `redirect_url` (pin the callback URL instead of deriving it from the request Host) |
| `service_tokens` | `[]` | `[{token, name, groups}]` for CI/cron |

## `access:` — grants

```yaml
access:
  roles:                          # capability sets (defaults shown)
    viewer:   [view]
    operator: [view, refresh]
    admin:    [view, refresh, manage]
  grants:
    - path: "*"            role: viewer   groups: [data]
    - path: "*"            role: operator groups: [data-ops]
    - path: finance/*      role: operator groups: [finance]
    - path: restricted/*   role: viewer   groups: [execs]
    - path: exec/board     role: admin    users: [ceo@corp.com]
```

Path patterns: `*` (everything), `folder/*` (subtree), `name` (exact board).
Matching rules:

- The **most specific pattern wins** and shadows broader ones — `restricted/*`
  carves a subtree out of a `*` grant (no fall-through).
- Grants on the **same pattern union** — one pattern can give different roles
  to different groups.
- No matching grant → **deny**. Every grant must name `groups` and/or `users`
  (matched against OIDC `sub`/`email`).
- Role names must exist in `roles:`.

## Full example

See the annotated `charts-tool.yml` at the repo root.
