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

Requires `refresh` on the board. `vars` are validated against the board's
declared variables before anything renders. Returns `200` when the outcome is
known (`cached`, or `done` with `wait: true`), `202` when work is queued/running.

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

Errors: `400` invalid board reference or unknown/invalid variable, `404`
unknown board, `413` request body over 1 MiB, `422` unknown format, `429`
per-identity render rate limit (`policy.max_renders_per_minute`), `502` render
failed (generic detail; the full stderr is logged server-side and attached to
the job record, visible to `refresh` grantees), `504` render timeout.

### `POST /api/warm`

Deploy-pipeline warm step: renders every board+variable combo declared in
`warm.boards` (config) so no viewer pays a cold cache miss after a deploy or
dct upgrade. Auto (non-force) semantics — unchanged boards come back `cached`
without querying the warehouse.

```json
{"wait": true}                         // optional body; false submits and returns 202
```

Requires `refresh`, checked per board — a denied board fails its own entry,
not the batch. Returns `200` with per-entry outcomes; any failed entry
(unknown board, missing grant, invalid vars, render error, rate limit) flips
the response to `502` so `curl -f` fails the pipeline — all entries are still
reported. Entries still queued or rendering when the wait window closes report
`running` with their job id and do not fail the batch; the artifact lands on
its own.

```json
{
  "ok": true,
  "results": [
    {"board": "sales_daily", "vars": {}, "key": "ae96250e0926fa7d",
     "outcome": "done", "job_id": "9c1e7a2b4f0d4e8a", "duration_ms": 2177, "error": null}
  ]
}
```

CI usage: `curl -fsSL -X POST $HUB/api/warm -H "Authorization: Bearer $TOKEN"`.

## Render metadata & artifacts

### `GET /api/renders/{key}`
Artifact metadata (successful renders only — failures live on jobs). `404` if
unknown or not viewable (identical response either way — no existence oracle).
Requires `view` on the artifact's board.

### `GET /api/renders/{key}/artifact`
Raw artifact bytes with the format's media type. Requires `view` (same
unknown/unauthorized 404 shape).

## Jobs

### `GET /api/jobs` and `GET /api/jobs/{job_id}`
Job status: `queued | running | done | error | interrupted`, plus mode
(`auto`/`force`), `requested_by`, error tail, and timings. The error tail
carries dct/warehouse stderr, so it is returned only to callers with `refresh`
on the job's board; view-only callers get `error: null`. The list endpoint is
newest-first (`limit` 1-500, default 50) and filtered to boards the caller can
view. Unknown and unauthorized job ids return the same `404`.

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
happen. Both validate variables against the board's declarations first (`400`),
and render submissions are capped by `policy.max_renders_per_minute` (`429`;
over the limit, a stale artifact is simply served without the background
refresh). The stale-while-revalidate enqueue requires the `refresh` grant —
view-only identities are always just served the stale artifact. Responses
include `X-Dct-Hub-Key`, `X-Rendered-At`, and
`Cache-Control: no-cache`. HTML artifacts are served with
`Content-Security-Policy: sandbox allow-scripts` — chart JS runs, but the
artifact executes in an opaque origin with no cookies, storage, or API access
as the viewer (same for `GET /api/renders/{key}/artifact` when the format is
html or svg — both are script-capable when opened as a document). All responses carry `X-Content-Type-Options`, `Referrer-Policy`,
`X-Frame-Options`, and a baseline CSP.

## Auth endpoints

| Route | Purpose |
|---|---|
| `GET /auth/login?next=/...` | Start OIDC flow (400 if tokens-only) |
| `GET /auth/callback` | OIDC redirect target |
| `GET /auth/logout` | Clear session |
| `GET /auth/me` | Current identity JSON |

## Misc

`GET /healthz` → `{"status": "ok"}` (public, used by the Docker healthcheck).
