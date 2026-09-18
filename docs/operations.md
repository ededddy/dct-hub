# Operations guide

How the pieces chain together end-to-end: who owns what, which pipeline runs
what, and what to do when something changes or breaks. This is the map; the
details live in `architecture.md`, `deployment.md`, `configuration.md`, and
`api.md`.

## The chain at a glance

```
data team                    BI team                  CI/CD              dct-hub (server)
                                                                         
dbt models ──┐        charts/*.yml ──┐
             ▼                       ▼
     dbt build + test        dct validate --warehouse
     dct impact <col> ── contract ──► both green → merge
                                     │
                                     ▼ deploy pipeline
                               1. sync project → /project
                               2. curl -f POST /api/warm ──────► renders top boards
                                     │                            (auto: unchanged = cached)
                                     ▼
                          viewers ──► render on request
                                    (frozen / TTL / stale-while-revalidate)
```

The hub renders on demand — there is no build step that produces artifacts
ahead of time except the deliberate warm call. Everything else is cache
policy (`architecture.md` § Cache model).

## Ownership

| Surface | Owner | Notes |
|---|---|---|
| dbt models, warehouse | Data team | The contract boards depend on is table/column names |
| `charts/**.yml` | BI team | One file per board; folder structure = URL paths = grant patterns |
| `charts/meta.yml` chain | BI team | Shared board defaults (source, theme, style) |
| `dbt_charts.yml` | Joint | Sources registry; changing it re-fingerprints **every** board |
| `charts-tool.yml`, image, tokens | Platform/ops | Hub config, auth, retention, warm list |

## Pipeline 1 — dbt CI (data team)

1. `dbt build` / `dbt test` as usual.
2. For each renamed/dropped column in the diff, run
   `dct impact <column> [--table <model>] --json` in the **boards** repo. It
   walks compiled board SQL with no warehouse connection and lists which
   boards break (plus boards it can't prove safe — `SELECT *`, unparsable
   SQL — never read an empty hit list as safe without checking those).
3. Impact hits → the PR is a **breaking change for BI**: either fix the
   affected boards in the same change window, or sequence board PR first.
   This check is the contract between the two teams; run it on both sides.

## Pipeline 2 — boards CI (BI team)

Per pull request against the boards repo:

```bash
dct validate --warehouse      # schema + cross-refs + EXPLAIN/dry-run per query
dct render <changed boards>   # optional smoke: catches what dry-run can't
```

- `dct validate` alone is stateless (no DB); `--warehouse` adds the cheapest
  validity check the adapter offers (EXPLAIN, dry-run, DESCRIBE) — no query
  runs at full cost.
- A smoke render is optional but is the only check that executes SQL end to
  end. Point CI at a dev warehouse or a slim dataset.
- Keep variables low-cardinality (selects, dates). Every distinct variable
  combination is a separate cached artifact; a free-text variable defeats the
  cache (`README` § Freshness).

## Pipeline 3 — deploy (CD, on merge)

```bash
# 1. Get the project onto the server (any one of):
git -C /project pull            # or rsync, artifact unpack, image bake
#    Board discovery is a per-request filesystem scan — no hub restart.

# 2. Warm the declared boards (one call — in HA this warms every replica,
#    since metadata and artifacts are shared):
curl -fsSL -X POST "$HUB/api/warm" -H "Authorization: Bearer $DCT_HUB_CI_TOKEN"
```

- The warm call uses **auto semantics**: boards whose YAML changed have a new
  source fingerprint, hence a new cache key — they render; unchanged boards
  return `cached` for free. No cache flush, ever (D3, D17).
- `curl -f` fails on the 502 → pipeline goes red. The response body lists
  every board's outcome; a failed warm does **not** roll back the sync, and
  usually shouldn't — boards still serve (old artifacts where keys are
  unchanged), the failed boards just pay a cold miss on first view.
  Investigate via the per-board `error` field and `GET /api/jobs`.
- The CI identity is a service token (`auth.service_tokens`) whose groups map
  to a role with `refresh`. Its submissions count against
  `policy.max_renders_per_minute` — leave headroom for the warm list's size.
- Maintain `warm.boards` (config) as the top-N boards that must never be cold
  for a human. It's the operator's bound on warehouse cost — only listed
  boards render ahead of demand.

## Scheduled jobs — the exception, not the rule

Demand-driven rendering covers everything except one case: an **intraday
board** that must already be fresh whenever someone opens it (e.g. the 9am
exec view over today's filling data). Only that wants a schedule:

```bash
curl -fsSL -X POST "$HUB/api/renders" -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{"board": "sales_daily", "vars": {"day": "'$(date +%F)'"}, "force": true, "wait": true}'
```

cron, Kestra, Airflow — anything that can HTTP POST. If it's not intraday-
critical, don't schedule it; the TTL + stale-while-revalidate policy already
keeps viewers off the render path.

## Day-2 operations

**Monitor.** `GET /api/jobs` is the render audit trail (status, mode,
`requested_by`, error tail, timings — and `claimed_by` in HA, so you can see
which replica ran a job) — alert on `error` rate. Auth events log to stderr
under `dct_hub.auth` / `dct_hub.access`; ship them. `/healthz` for liveness
(the Docker healthcheck uses it); in HA, alert on 5xx rates too — `/healthz`
does not probe Postgres.

**Upgrade dct.** Bump `requirements-dbt.txt` → `scripts/vendor_wheels.sh` →
rebuild → redeploy (rolling, in HA) → `POST /api/warm`. The dct version is in
the cache key, so every board re-renders on demand after an upgrade; warming
absorbs that for the listed boards. Upgrade the hub image and dct together —
the lock covers both.

**Rollback a board.** `git revert` the YAML, re-sync, re-warm. The cache key
returns to a value that likely still has an artifact on disk (retention
permitting) → instant serve of the previous version.

**Retention.** Off by default. Enable (`retention.enabled: true`) once
comfortable; frozen snapshots are never pruned and unclassifiable artifacts
are always kept (`configuration.md` § retention).

## Failure modes

| What breaks | What viewers see | What to do |
|---|---|---|
| Render fails (bad SQL, warehouse down) | Cached artifacts keep serving; a board with no artifact gets 502 in the iframe | `GET /api/jobs` error tail; fix board/warehouse; re-warm |
| Warm call fails | Pipeline red; deploy still live | Read per-board errors in the response body; usually a board/warehouse/grant issue, not a rollback trigger |
| `dct` binary broken/missing | Serving of existing artifacts continues; new renders 502 | Restore the binary in the image; no data lost |
| Hub restarts (single-node) | Brief outage; state persists in the volume; in-flight jobs marked `interrupted` | Nothing — re-warm if the restart followed a deploy |
| A replica dies (HA) | LB routes to survivors; jobs it claimed are reaped to `interrupted` within a minute; viewers retry on another replica | Nothing automatic — check the dead node's logs |
| Postgres unreachable (HA) | Store-backed pages and renders 502 cluster-wide; `/healthz` stays green (process liveness, not readiness) | Restore PG — pools reconnect on their own; alert on 5xx rate, don't rely on the LB check alone |
| Bad board deployed | That board errors; every other board unaffected | Revert + re-sync + re-warm (see Rollback) |
| Warm lists a deleted board | 502 with per-board `board not found` | Remove it from `warm.boards` |

## Golden rules

1. **Never flush the cache.** Keys are content-addressed; deploys and
   upgrades invalidate by construction. There is no flush endpoint and none
   is needed.
2. **Renders happen through the hub's API, never `dct render` on the side** —
   a CLI render writes elsewhere and never lands in the artifact store the
   hub serves from.
3. **Failed renders never displace a good artifact** (D6). You can always
   re-warm safely.
4. **Warehouse credentials in env/secrets, never in YAML** — boards reference
   sources by name only.
