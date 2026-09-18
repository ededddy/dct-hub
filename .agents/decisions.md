# Decision record

Load-bearing choices made while building dct-hub, with rationale and rejected
alternatives. Read this before changing the architecture. For how things fit
together, see `docs/architecture.md`.

## Serving & rendering

### D1. Artifact store, not a proxy over `dct serve`
Boards are rendered via `dct render` to stored HTML artifacts; there is no
live `dct serve` exposure at all.
**Why:** frozen snapshots fall out naturally (immutable files), authorization
can be enforced on every byte served, and no live warehouse access path exists
outside the render queue.
**Rejected:** proxying `dct serve` (interactive but authz-unenforceable on its
internal routes, no snapshot semantics); hybrid (too many moving parts).

### D2. Shell out to the dct CLI; never import dbt-charts
All rendering/description/search is `dct ...` in a subprocess.
**Why:** the hub and dct evolve independently; dct is version-pinned in
`requirements-dbt.txt`; subprocess isolation contains hangs/crashes
(`render.timeout_s`).

### D3. Cache key = board + vars + source fingerprint + format + dct version
The fingerprint hashes the board file, every `meta.yml` from `charts/` down to
the board's directory (meta deep-merges into boards), and `dbt_charts.yml`
(theme/sources change output bytes).
**Why:** stale artifacts are the worst failure mode for a cache.
**Consequence:** data changes alone never invalidate — refresh is explicit
(`force`) or TTL-driven. This is deliberate.

## Freshness

### D4. Frozen = all date-picker variables in the past
Determined from `dct describe` variable metadata (`input: date/datepicker`),
including defaults for unset vars. Malformed/future/unset dates → TTL path.
`daterange` is NOT treated as frozen (ambiguous value shape over the API).
**Why:** "yesterday's report never changes" is the cheapest possible cache.

### D5. Fail-safe asymmetry: serving degrades, pruning never does
If `dct describe` fails: freshness falls back to TTL (serving must not break),
but retention treats the artifact as unclassifiable and **keeps** it.
**Why:** the cost of a wrong keep is disk; the cost of a wrong delete is
breaking the immutability promise.

### D6. Only successful renders in the `renders` table
Errors live on `jobs`. (M3 regression: an error record written with
`INSERT OR REPLACE` displaced the last good artifact row and took the board
offline for viewers.)
**Invariant:** a failed refresh must never displace the last good artifact.

## Access control

### D7. Most-specific path shadows; same-pattern grants union
`restricted/*` overrides `*` for its subtree (no fall-through — carve-outs
must be possible), while multiple grants on the identical pattern combine.
**Why:** fall-through semantics made `restricted/*` useless; first-match-wins
made `*` viewer + `*` operator-by-group unwritable.
**Rejected:** longest-prefix with identity fall-through; first-match-wins.

### D8. `refresh` capability gates every warehouse query
Including render-on-miss page views and the UI refresh button. Viewers can
only ever see already-rendered artifacts (plus system-driven stale-while-
revalidate, bounded by TTL + `min_interval_s`).
**Why:** an unauthenticated/unprivileged "view" must never cost warehouse work.

### D9. Hand-rolled OIDC (auth-code + PKCE) with joserfc; no OAuth framework
Lazy discovery (hub boots while IdP is down), state/nonce/PKCE in the signed
session cookie, RS256 validation against JWKS, one JWKS refetch on validation
failure (survives Keycloak key rotation), all token errors → clean 401.
**Why:** air-gapped reviewability; authlib's `authlib.jose` is deprecated in
favor of `joserfc`, so we depend on `joserfc` directly.
**Conventions:** `azp` unchecked (single-aud Keycloak default); session is
signed-not-encrypted with cookie `max_age` only (no server-side revocation).

## Queue & concurrency

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
allowlisted (`RenderRequest` validator) and `new_artifact_path` re-checks it
before building the on-disk path.
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

## Packaging (air-gapped)

### D12. Hash verification at vendor time; version resolution at image build
`scripts/vendor_wheels.sh` verifies every download against the hashed lock
(and pre-builds wheels from sdist-only packages). The Dockerfile installs
`--no-index` by pinned version from the vendored set, without re-checking
hashes — locally-built wheels can't match their sdists' hashes by definition.
### D13. All UI assets inline; no CDN; no SPA build
Three Jinja templates with inline CSS/JS. Air-gapped browsers can't fetch
external assets, and the surface is small enough that a frontend pipeline
would be ceremony.

## Conventions for agents working here

- **Version lives in three places** — `pyproject.toml`, `src/dct_hub/__init__.py`,
  and the `FastAPI(version=...)` string in `src/dct_hub/api.py`. Bump all three
  (it drifted once).
- **Tests inject fakes by attribute swap**: `app.state.service.dct = FakeDct()`
  — keep service attributes late-bound (an early-bound `describe=self.dct.describe`
  reference broke this once).
- **Run tests**: `.venv/bin/python -m pytest -q`. The e2e test needs the real
  `dct` binary and skips cleanly without it.
- **`tests/fake_idp.py`** is a full in-process OIDC provider — use it for any
  auth-flow test; never mock the JWT layer.
- **URL contract**: `/b/<board>` = shell page, `/raw/<board>` = artifact,
  `/api/*` = JSON. Authz must always run before board-existence resolution
  (no 404-vs-401 probing).
- **Sample data**: `python sample_project/scripts/gen_data.py [--complete-today]`
  — deterministic; past days never change between runs.
