# Decisions: serving & rendering

| # | Where it lives |
|---|---|
| D1 | `src/dct_hub/store.py`, `src/dct_hub/blobs.py`, artifact routes in `src/dct_hub/api.py` |
| D2 | `src/dct_hub/dct.py` |
| D3 | `src/dct_hub/cachekey.py` |

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
