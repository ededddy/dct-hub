# Decisions: queue & concurrency

| # | Where it lives |
|---|---|
| D10 | `src/dct_hub/queue.py`, `src/dct_hub/store.py` (`LocalStore`) |
| D11 | `src/dct_hub/dct.py` (`_var_arg`), `RenderRequest.vars` in `src/dct_hub/api.py` |
| D14 | `validate_vars` in `src/dct_hub/api.py` |
| D15 | `SANDBOX_CSP` in `src/dct_hub/api.py`, `src/dct_hub/templates/board.html` |
| D16 | `src/dct_hub/ratelimit.py`, `PgRateLimiter` in `src/dct_hub/store_pg.py` |
| D17 | `warm` / `warm_submit` / `warm_await` in `src/dct_hub/api.py`, `WarmConfig` in `src/dct_hub/config.py` |
| D18 | `src/dct_hub/store_pg.py`, `src/dct_hub/blobs.py`, `src/dct_hub/queue.py`, `janitor_loop` in `src/dct_hub/gc.py` |

### D10. Single-process queue, SQLite as the job log
In-process asyncio workers; jobs persisted for audit; single-flight via
check-then-insert with no awaits in between; crashed jobs → `interrupted` on
next start. Concurrency forced to 1 when `render.query_cache` is set (DuckDB
single-writer).
**Rejected:** external queue (Redis/RQ) — operational overkill for one
container; multi-process single-flight — would need an external lock.
**Superseded for HA mode by D18:** with `storage.postgres` set, the queue is
cluster-wide (single-flight by unique index, SKIP LOCKED claims). This entry
still describes the default single-node mode.

### D18. HA mode: externalized state in Postgres + S3, replicas stateless
With `storage.postgres` set, every replica is stateless: metadata, job queue,
rate limit and leadership live in Postgres; artifact blobs live in S3-compatible
storage (MinIO for air-gapped). No Redis — queue/lock/limit QPS is low, and the
jobs table, advisory locks, and a hits table cover all three.
Load-bearing semantics:
- **Single-flight by schema**: a partial unique index on
  `jobs(key) WHERE status IN ('queued','running')`; insert conflict = coalesce.
  No cross-node check-then-insert race.
- **Claims**: `FOR UPDATE SKIP LOCKED`, stamped `claimed_by = hostname:pid`. A
  booting replica interrupts only its own node id; a janitor (advisory-lock
  leader) reaps running jobs older than `render.timeout_s + 120s` — orphans
  from dead replicas.
- **Blob/row invariant**: publish blob before its renders row; delete row
  before blob. A row always implies the blob exists, so serving trusts
  metadata (no per-request HEAD on the hot path beyond the exists preflight).
- **Serving streams through the hub** from S3 — per-request grants and the
  sandbox CSP still apply to every byte (D1). Presigned URLs rejected: they
  bypass authorization.
- **`wait` is event + poll**: instant for locally-executed jobs, 0.5s poll
  discovers jobs finished by other replicas.
- Local mode is untouched: SQLite + filesystem remains the default and the
  dev loop; switching modes starts a fresh store (re-warm after switching).
**Rejected:** Redis for queue/limiter (third infra system, no capability we
lack); presigned S3 URLs (authz bypass); shared-NFS artifacts (SQLite-over-NFS
locking, torn reads); per-replica rate limiting (N × configured cap).

### D11. Scalar variables only over the API
Vars are stringified scalars (`--var k=v`); `""` means unset. Multiselect/
daterange need typed handling and currently degrade to text inputs in the UI.
**Known limitation** — revisit when a board needs them.

### D14. Render-bound variables validated against `dct describe`
Every path that can start a render (`POST /api/renders`, `/raw` render-on-miss
and stale-while-revalidate, the `/b` shell) validates vars against the board's
declared variables: unknown names, out-of-list options, and ill-typed
date/number/checkbox values get 400; `describe` unavailable gets 502 (fail
closed — unvalidated input never reaches the warehouse). Serving a cached
artifact never validates (D5 asymmetry preserved). The request `format` is
allowlisted (`RenderRequest` validator) and the blob locator builder re-checks
it before building the on-disk path.
**Why:** boards may interpolate vars raw into SQL; the hub can't quote for
them, but it can shrink the injection surface to declared, typed inputs.
Free-text vars remain the board author's quoting responsibility.

### D15. Rendered artifacts served into a sandboxed origin
`/raw/` and html/svg artifact responses carry `Content-Security-Policy:
sandbox allow-scripts`, and the board iframe has `sandbox="allow-scripts"`. Chart JS
runs, but artifacts execute in an opaque origin: no cookies, no storage, no
API calls as the viewing user. The refresh button reloads the iframe by
resetting its `src` from the parent — cross-origin `contentWindow.location`
is off-limits. A baseline CSP + `X-Content-Type-Options` / `Referrer-Policy` /
`X-Frame-Options` middleware covers everything else (`unsafe-inline` is
unavoidable under D13).

### D16. Opt-in per-identity render rate limit
`policy.max_renders_per_minute` (default 0 = unlimited) caps render
submissions per identity across auto and force — this is the bound on force's
documented `min_interval_s` bypass. In-memory sliding window (single process,
D10); resets on restart, acceptable for an abuse cap. Stale-while-revalidate
over the limit skips the refresh and serves stale rather than failing the
page. Default-off so existing cron/Airflow automation is unaffected.
In HA mode (D18) the limiter runs on a shared `rate_hits` table, so the cap is
cluster-wide; count-then-insert across nodes can overshoot slightly — still an
abuse cap, not a quota.

### D17. Deploy-time warming via config + `POST /api/warm`, auto semantics
The warm list is declared in `charts-tool.yml` (`warm.boards`) and executed by
one authenticated pipeline call after a deploy or dct upgrade. Warming uses
auto (non-force) semantics: a board edit changes the source fingerprint, hence
the artifact key, so changed boards miss the cache on their own while
unchanged boards report `cached` and cost no warehouse query. Per-board
failures aggregate into HTTP 502 so `curl -f` fails the pipeline; a render
still going past the wait window reports `running` (with its job id) without
failing — the artifact lands on its own.
**Why:** the operator's config bounds warehouse cost (only listed boards and
combos render), and server-side execution reuses the queue, single-flight,
grants, and rate limiting — no client logic to version across consumers.
**Rejected:** force-rendering the warm list (re-queries unchanged boards on
every deploy); a repo-shipped CI script calling `/api/renders` per board
(error aggregation and auth duplicated in every consumer); filesystem
auto-watch (surprise warehouse load on mass edits — the pipeline call is
explicit).
