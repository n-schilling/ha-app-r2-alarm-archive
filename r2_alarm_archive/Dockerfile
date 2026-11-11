# Pinned by version and digest, so a moved tag cannot change the image unnoticed;
# Dependabot proposes updates
FROM ghcr.io/home-assistant/base-python:3.14-alpine3.24-2026.11.0@sha256:aafc599627b1ea2c25c154cbd44867fefe024c12c38a2a355d5e8a4d1b6a7bdd

SHELL ["/bin/ash", "-o", "pipefail", "-c"]

# requirements.txt pins every package with its hashes (uv pip compile from
# requirements.in), so a build installs exactly the files reviewed
COPY requirements.txt /tmp/
RUN pip3 install --no-cache-dir --only-binary=:all: --require-hashes -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt \
    && addgroup -S r2archive \
    && adduser -S -D -H -G r2archive -h /nonexistent -s /sbin/nologin r2archive

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
