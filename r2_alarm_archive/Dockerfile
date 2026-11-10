# A fixed tag, so every build starts from the same image; Dependabot proposes new ones
FROM ghcr.io/home-assistant/base-python:3.14-alpine3.24-2026.11.0

SHELL ["/bin/ash", "-o", "pipefail", "-c"]

COPY requirements.txt /tmp/
RUN pip3 install --no-cache-dir --only-binary=:all: -r /tmp/requirements.txt \
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
