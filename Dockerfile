# Air-gapped image: everything installs from vendored wheels, no index access.
# Build prep (on a networked machine):  scripts/vendor_wheels.sh
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DCT_HUB_CONFIG=/config/charts-tool.yml

WORKDIR /app

COPY requirements.lock /tmp/requirements.lock
COPY image-requirements.txt /tmp/image-requirements.txt
COPY wheels/ /tmp/wheels/
COPY dist/dct_hub-*.whl /tmp/
# Hashes are verified at vendor time (pip download checks every file against
# requirements.lock); the image install resolves the same pinned versions
# from the vendored set with no index access.
RUN pip install --no-cache-dir --no-index --find-links /tmp/wheels \
      '/tmp/dct_hub-*.whl[ha]' -r /tmp/image-requirements.txt \
    && rm -rf /tmp/wheels /tmp/requirements.lock /tmp/image-requirements.txt /tmp/dct_hub-*.whl

RUN useradd --create-home --uid 10001 dcthub \
    && mkdir -p /state \
    && chown -R dcthub:dcthub /state
USER dcthub

# Mount points: the dbt Charts project (boards) and hub state.
#   docker run -v $PWD/charts-tool.yml:/config/charts-tool.yml:ro \
#              -v $PWD/my-project:/project -v dct-hub-data:/state dct-hub
VOLUME ["/state"]

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
  CMD ["python", "-c", "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4)"]

CMD ["sh", "-c", "exec dct-hub --config \"$DCT_HUB_CONFIG\""]
