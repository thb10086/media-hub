# syntax=docker/dockerfile:1.7
# DTK with patches/dtk/*.patch applied, built on top of the published release
# image instead of from source: building DTK means compiling its web console
# with npm, which a 4 GB server should not be doing.
#
# The build context is the patched checkout of the SAME commit the base image
# was built from (deploy/versions.env pins both). The published image installs
# dtk non-editable into /app/.venv, so the patched package is copied over it.
ARG DTK_BASE_IMAGE=evil0ctal/douyin_tiktok_download_api:5.1.1
FROM ${DTK_BASE_IMAGE}

ARG DTK_SITE_PACKAGES=/app/.venv/lib/python3.12/site-packages

USER root
RUN test -f "${DTK_SITE_PACKAGES}/dtk/worker/registry.py"
COPY --chown=root:root src/dtk "${DTK_SITE_PACKAGES}/dtk"
# Drop bytecode compiled from the unpatched sources, then prove the patch is live.
RUN find "${DTK_SITE_PACKAGES}/dtk" -name __pycache__ -type d -prune -exec rm -rf {} + \
 && python -m compileall -q "${DTK_SITE_PACKAGES}/dtk" \
 && python -c "from dtk.worker import registry; assert {'douyin.search', 'douyin.hot_search'} <= set(registry.ENDPOINTS)"
USER dtk
