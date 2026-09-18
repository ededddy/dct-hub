#!/usr/bin/env bash
# Vendor everything the air-gapped image needs:
#   requirements.lock  pinned + hashed deps (dct-hub deps + dbt-charts)
#   wheels/            manylinux wheels for the lock (sdists pre-built to wheels)
#   dist/              the dct-hub wheel itself
#
# Run on any machine with network access, commit requirements.lock (and keep
# wheels/ + dist/ out of git), then `docker build` works with --no-index.
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON_VERSION="${PYTHON_VERSION:-3.13}"
PLATFORM="${PLATFORM:-x86_64-unknown-linux-gnu}"   # aarch64-unknown-linux-gnu for ARM
ARCH="${PLATFORM_ARCH:-x86_64}"                    # aarch64 for ARM

uv pip compile pyproject.toml requirements-dbt.txt \
  --generate-hashes \
  --python-version "$PYTHON_VERSION" \
  --python-platform "$PLATFORM" \
  -o requirements.lock

# single source of truth for the image's dbt-charts pin
grep -m1 '^dbt-charts==' requirements.lock | tr -d ' \\' > image-requirements.txt

rm -rf wheels wheels_raw dist
mkdir -p wheels wheels_raw

shopt -s nullglob
PIP_PLATFORM_ARGS=()
if [[ -n "${PIP_PLATFORMS:-}" ]]; then
  for tag in $PIP_PLATFORMS; do PIP_PLATFORM_ARGS+=(--platform "$tag"); done
else
  PIP_PLATFORM_ARGS=(--platform "manylinux_2_28_$ARCH" --platform "manylinux2014_$ARCH")
fi

# Fetch binaries where available plus sdists for the sdist-only packages
# (pip needs network here; the image build later does not). --no-deps: the
# lock already contains the full transitive closure.
python3 -m pip download -r requirements.lock -d wheels_raw \
  --no-deps \
  --python-version "$PYTHON_VERSION" \
  --implementation cp \
  --abi "cp${PYTHON_VERSION/./}" --abi abi3 --abi none \
  "${PIP_PLATFORM_ARGS[@]}" \
  --quiet

mv wheels_raw/*.whl wheels/
sdists=(wheels_raw/*.tar.gz)
if ((${#sdists[@]})); then
  echo "building wheels from ${#sdists[@]} sdist(s): ${sdists[*]##*/}"
  python3 -m pip wheel --no-deps -w wheels "${sdists[@]}" --quiet
fi
rm -rf wheels_raw

uv build --wheel -o dist

echo "vendored $(ls wheels | wc -l | tr -d ' ') wheels + $(ls dist)"
