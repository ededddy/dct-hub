# dct-hub API reference

Base URL: `http://<host>:8080`. When auth is enabled, all endpoints require
credentials except `/healthz` and `/auth/*`:

- **Humans**: OIDC session cookie (browser flow via `/auth/login`).
- **Machines**: `Authorization: Bearer <token>` (configured service tokens).

Capabilities: `view` (reads) and `refresh` (anything that runs warehouse
queries). Anonymous API callers get `401`; authenticated-but-unauthorized get
`403`. Board existence is never revealed before authorization.

## Render triggers

### `POST /api/renders`

```json
{
  "board": "sales_daily",            // "charts/sales_daily.yml" also accepted
  "vars": {"day": "2026-09-16"},     // string values; "" means unset
  "format": "html",                  // html | svg | png | pdf | json | yaml
  "force": false,                    // bypass TTL/frozen cache, always re-render
  "wait": false                      // block until the job finishes
}
```

Requires `refresh` on the board. Returns `200` when the outcome is known
(`cached`, or `done` with `wait: true`), `202` when work is queued/running.

Response:

```json
{
  "key": "ae96250e0926fa7d",
  "board": "sales_daily",
  "vars": {"day": "2026-09-16"},
  "format": "html",
  "outcome": "cached | queued | running | done",
  "cached": true,
  "stale": false,                    // served stale due to min_interval rate limit
  "job_id": null,
  "duration_ms": 2177,
  "rendered_at": "2026-09-17T08:44:34+00:00",
  "url": "/b/sales_daily?day=2026-09-16",
  "artifact_url": "/api/renders/ae96250e0926fa7d/artifact"
}
```

Errors: `400` invalid board reference, `404` unknown board, `502` render failed
(detail carries the dct stderr tail), `504` render timeout.

## Render metadata & artifacts

### `GET /api/renders/{key}`
Artifact metadata (successful renders only — failures live on jobs). `404` if
unknown. Requires `view` on the artifact's board.

### `GET /api/renders/{key}/artifact`
Raw artifact bytes with the format's media type. Requires `view`.

## Jobs

### `GET /api/jobs` and `GET /api/jobs/{job_id}`
Job status: `queued | running | done | error | interrupted`, plus mode
(`auto`/`force`), `requested_by`, error tail, and timings. The list endpoint
is newest-first (limit default 50) and filtered to boards the caller can view.

## Catalog

### `GET /api/boards`
`[{board, title, notes}]`, filtered to the caller's viewable boards.

### `GET /api/boards/{board}`
`dct describe --json` passthrough: queries, charts, variables (names, input
types, defaults, options), layout. Requires `view`.

## Pages (browser)

| Route | Purpose |
|---|---|
| `GET /` | Catalog home: search, freshness badges, favorites/recents |
| `GET /b/{board}?var=...` | Board shell: variable controls, freshness, refresh button, snapshot history |
| `GET /raw/{board}?var=...` | The rendered artifact HTML (what the iframe loads) |

`/raw/` is where render-on-miss (needs `refresh`) and stale-while-revalidate
happen. Responses include `X-Dct-Hub-Key`, `X-Rendered-At`, and
`Cache-Control: no-cache`.

## Auth endpoints

| Route | Purpose |
|---|---|
| `GET /auth/login?next=/...` | Start OIDC flow (400 if tokens-only) |
| `GET /auth/callback` | OIDC redirect target |
| `GET /auth/logout` | Clear session |
| `GET /auth/me` | Current identity JSON |

## Misc

`GET /healthz` → `{"status": "ok"}` (public, used by the Docker healthcheck).
