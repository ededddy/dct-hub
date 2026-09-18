# Decision record

Load-bearing choices made while building dct-hub, with rationale and rejected
alternatives. Read the relevant file before changing that part of the
architecture; each file opens with a code map. For how things fit together,
see `docs/architecture.md`.

| # | Decision | File |
|---|---|---|
| D1 | Artifact store, not a proxy over `dct serve` | [serving](serving.md) |
| D2 | Shell out to the dct CLI; never import dbt-charts | [serving](serving.md) |
| D3 | Cache key = board + vars + fingerprint + format + dct version | [serving](serving.md) |
| D4 | Frozen = all date-picker variables in the past | [freshness](freshness.md) |
| D5 | Fail-safe asymmetry: serving degrades, pruning never does | [freshness](freshness.md) |
| D6 | Only successful renders in the `renders` table | [freshness](freshness.md) |
| D7 | Most-specific path shadows; same-pattern grants union | [access control](access-control.md) |
| D8 | `refresh` capability gates every warehouse query | [access control](access-control.md) |
| D9 | Hand-rolled OIDC with joserfc; no OAuth framework | [access control](access-control.md) |
| D10 | Single-process queue, SQLite as the job log (single-node mode) | [queue](queue-concurrency.md) |
| D11 | Scalar variables only over the API | [queue](queue-concurrency.md) |
| D12 | Hash verification at vendor time; version resolution at image build | [packaging](packaging.md) |
| D13 | All UI assets inline; no CDN; no SPA build | [packaging](packaging.md) |
| D14 | Render-bound variables validated against `dct describe` | [queue](queue-concurrency.md) |
| D15 | Rendered artifacts served into a sandboxed origin | [queue](queue-concurrency.md) |
| D16 | Opt-in per-identity render rate limit | [queue](queue-concurrency.md) |
| D17 | Deploy-time warming via config + `POST /api/warm`, auto semantics | [queue](queue-concurrency.md) |
| D18 | HA mode: externalized state in Postgres + S3, replicas stateless | [queue](queue-concurrency.md) |

New decisions get the next number (D19+), in the file that fits; never
renumber — docs and commit messages cite D-numbers.

## Conventions for agents working here

- **Version lives in three places** — `pyproject.toml`, `src/dct_hub/__init__.py`,
  and the `FastAPI(version=...)` string in `src/dct_hub/api.py`. Bump all three
  (it drifted once).
- **Tests inject fakes by attribute swap**: `app.state.service.dct = FakeDct()`
  — keep service attributes late-bound (an early-bound `describe=self.dct.describe`
  reference broke this once).
- **Run tests**: `.venv/bin/python -m pytest -q`. The e2e test needs the real
  `dct` binary and skips cleanly without it; Postgres HA tests gate on
  `DCT_HUB_TEST_PG_DSN`.
- **`tests/fake_idp.py`** is a full in-process OIDC provider — use it for any
  auth-flow test; never mock the JWT layer.
- **URL contract**: `/b/<board>` = shell page, `/raw/<board>` = artifact,
  `/api/*` = JSON. Authz must always run before board-existence resolution
  (no 404-vs-401 probing).
- **Sample data**: `python sample_project/scripts/gen_data.py [--complete-today]`
  — deterministic; past days never change between runs.
