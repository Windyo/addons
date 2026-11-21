# Web search add-on for HAOS (amd64): SearXNG + a small MCP server for the local LLM.
FROM python:3.13-slim-bookworm

ARG BUILD_VERSION=dev
# SearXNG has no releases; pinned to the commit of the image tag 2026.11.14-a7128d5d1.
ARG SEARXNG_COMMIT=a7128d5d1d5af2837a68fbeaaee9c3e8391aa9cf

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

# SearXNG runs from its source tree (python -m searx.webapp) in its own venv.
RUN set -eux; \
    git clone --filter=blob:none https://github.com/searxng/searxng.git /opt/searxng/src; \
    git -C /opt/searxng/src checkout --quiet "${SEARXNG_COMMIT}"; \
    python -m venv /opt/searxng/venv; \
    /opt/searxng/venv/bin/pip install --only-binary=:all: -r /opt/searxng/src/requirements.txt; \
    rm -rf /opt/searxng/src/.git

COPY requirements-mcp.txt /tmp/requirements-mcp.txt
RUN python -m venv /opt/mcp \
    && /opt/mcp/bin/pip install -r /tmp/requirements-mcp.txt \
    && rm /tmp/requirements-mcp.txt

COPY rootfs/ /
RUN chmod 0755 /usr/local/bin/*.sh /usr/local/bin/*.py

LABEL io.hass.type="addon" \
      io.hass.arch="amd64" \
      io.hass.version="${BUILD_VERSION}" \
      io.hass.name="Web Search" \
      org.opencontainers.image.source="https://github.com/windyo/addons"

CMD ["/usr/local/bin/run.sh"]
