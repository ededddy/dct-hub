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
| `dir` | `.hub` | Artifacts (`artifacts/`) + metadata DB (`meta.db`) |

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
| `max_renders_per_minute` | `0` | Per-identity cap on render submissions per minute, across auto and force. `0` = unlimited |

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

## `auth:`

| Key | Default | Purpose |
|---|---|---|
| `enabled` | `false` | Master switch. Off = anonymous full access (dev loop) |
| `session_secret` | — | Cookie signing key, `${ENV_VAR}`. Required when enabled; the dev default is rejected |
| `session_https_only` | `false` | Set `true` behind TLS (production) |
| `session_max_age_s` | `43200` | Session cookie lifetime |
| `oidc` | unset | Keycloak: `issuer` (realm URL), `client_id`, `client_secret`, `scopes`, `groups_claim` |
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
