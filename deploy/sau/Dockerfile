# syntax=docker/dockerfile:1.7
# social-auto-upload, backend only (no Vue console). Built by deploy/compose.yml
# with the pinned upstream checkout passed in as the `sau_src` build context.
#
# Upstream is midway through a playwright -> patchright migration, and neither
# of its dependency lists covers what the web backend imports:
#   * uploader/*          -> patchright (declared in pyproject.toml)
#   * myUtils/login, auth -> playwright + xhs (only in the older requirements.txt)
# Both are installed, each with the browser build it drives.
#
# Debian bookworm on purpose: playwright 1.52's `install-deps` predates trixie.
ARG PYTHON_IMAGE=python:3.10-slim-bookworm
FROM ${PYTHON_IMAGE}

ARG PIP_INDEX_URL=https://pypi.org/simple
ARG PLAYWRIGHT_VERSION=1.52.0
ARG XHS_VERSION=0.2.13

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_INDEX_URL=${PIP_INDEX_URL} \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright

WORKDIR /app
COPY --from=sau_src . /app

# conf.py is listed in pyproject's py-modules, so it has to exist before install.
RUN cp conf.example.py conf.py \
 && pip install ".[web]" "playwright==${PLAYWRIGHT_VERSION}" "xhs==${XHS_VERSION}" \
 && sed -i 's|deb.debian.org|mirrors.cloud.tencent.com|g' /etc/apt/sources.list.d/debian.sources \
 && sed -i 's|https://cdn.playwright.dev/dbazure/download/playwright|https://cdn.npmmirror.com/binaries/playwright|' /usr/local/lib/python3.10/site-packages/patchright/driver/package/lib/server/registry/index.js \
 && sed -i '/function cftUrl/,/};/ s|https://cdn.playwright.dev|https://cdn.npmmirror.com/binaries/chrome-for-testing|; /function cftUrl/,/};/ s|builds/cft/\${browserVersion}/\${suffix}|\${browserVersion}/\${suffix}|' /usr/local/lib/python3.10/site-packages/patchright/driver/package/lib/server/registry/index.js \
 && patchright install --with-deps chromium \
 && playwright install chromium-headless-shell \
 && apt-get update \
 && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/* \
 && mkdir -p /app/videoFile /app/cookiesFile \
 && chmod 1777 /app/videoFile

COPY --chmod=0755 entrypoint.sh /usr/local/bin/sau-entrypoint
COPY patch-login.py /usr/local/share/sau-patches/patch-login.py

EXPOSE 5409
ENTRYPOINT ["sau-entrypoint"]
