# Pinned by version and digest, so a moved tag cannot change the image unnoticed;
# Dependabot proposes updates
FROM ghcr.io/home-assistant/base-python:3.15-alpine3.24-2026.11.0@sha256:31e73797b8c9a0ba62d33f0f46f81174b808fb42bdcb2a902cb0e8c3efdfd8fc

SHELL ["/bin/ash", "-o", "pipefail", "-c"]

# requirements.txt pins every package with its hashes (uv pip compile from
# requirements.in), so a build installs exactly the files reviewed
COPY requirements.txt /tmp/
RUN pip3 install --no-cache-dir --only-binary=:all: --require-hashes -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

COPY rootfs /
RUN python3 -m compileall -q /usr/lib/r2_archive

ARG BUILD_ARCH BUILD_DATE BUILD_DESCRIPTION BUILD_NAME BUILD_REF BUILD_REPOSITORY BUILD_VERSION
# The version from config.yaml: for the MQTT discovery and the bottom of the web page
ENV APP_VERSION=${BUILD_VERSION}
# s6-overlay: warnings and errors only, no per-service start/stop info lines
ENV S6_VERBOSITY=1
LABEL \
    io.hass.name="${BUILD_NAME}" \
    io.hass.description="${BUILD_DESCRIPTION}" \
    io.hass.arch="${BUILD_ARCH}" \
    io.hass.type="addon" \
    io.hass.version=${BUILD_VERSION}
