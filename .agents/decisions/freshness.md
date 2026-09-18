# Decisions: freshness

| # | Where it lives |
|---|---|
| D4 | `src/dct_hub/policy.py` (`CachePolicy.freshness`, `_all_in_past`) |
| D5 | `src/dct_hub/policy.py` (TTL fallback vs `frozen()` tri-state), `src/dct_hub/gc.py` (`_keeps`) |
| D6 | `execute_render` in `src/dct_hub/api.py`, job finish in `src/dct_hub/queue.py` |

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
