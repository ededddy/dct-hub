# Decisions: packaging (air-gapped)

| # | Where it lives |
|---|---|
| D12 | `scripts/vendor_wheels.sh`, `Dockerfile`, `requirements.lock` |
| D13 | `src/dct_hub/templates/` |

### D12. Hash verification at vendor time; version resolution at image build
`scripts/vendor_wheels.sh` verifies every download against the hashed lock
(and pre-builds wheels from sdist-only packages). The Dockerfile installs
`--no-index` by pinned version from the vendored set, without re-checking
hashes — locally-built wheels can't match their sdists' hashes by definition.

### D13. All UI assets inline; no CDN; no SPA build
Three Jinja templates with inline CSS/JS. Air-gapped browsers can't fetch
external assets, and the surface is small enough that a frontend pipeline
would be ceremony.
