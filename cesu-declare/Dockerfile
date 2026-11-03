# CESU Declare add-on for HAOS (amd64).
# Official Playwright image (Ubuntu 24.04 "noble"): Chromium and all its system libraries are
# preinstalled, together with the matching Playwright Python package. The tag is pinned and must
# match rootfs/app/requirements.txt.
FROM mcr.microsoft.com/playwright/python:v1.59.0-noble

ARG BUILD_VERSION=dev

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LANG=C.UTF-8

COPY rootfs/app/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --break-system-packages -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

COPY rootfs/ /

LABEL io.hass.type="addon" \
      io.hass.arch="amd64" \
      io.hass.version="${BUILD_VERSION}" \
      io.hass.name="CESU Declare" \
      org.opencontainers.image.source="https://github.com/windyo/addons"

WORKDIR /app
CMD ["python3", "-u", "/app/main.py"]
