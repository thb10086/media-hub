# syntax=docker/dockerfile:1.7
# One image, three roles (compose picks the command):
#   gateway  uvicorn hub.main:create_app --factory   (default CMD)
#   worker   python -m hub.worker
#   migrate  media-hub init-db
ARG PYTHON_IMAGE=python:3.12-slim-bookworm

FROM ${PYTHON_IMAGE} AS build
ARG PIP_INDEX_URL=https://pypi.org/simple
ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_INDEX_URL=${PIP_INDEX_URL}
WORKDIR /src
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m venv /opt/venv && /opt/venv/bin/pip install .

FROM ${PYTHON_IMAGE} AS runtime
ARG HUB_UID=10001
RUN groupadd --system --gid "${HUB_UID}" hub \
 && useradd --system --uid "${HUB_UID}" --gid hub --home-dir /app --shell /usr/sbin/nologin hub \
 && install -d -o hub -g hub -m 0750 /app /data /data/media /data/sau-videos
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HUB_MEDIA_DIR=/data/media
COPY --from=build /opt/venv /opt/venv
WORKDIR /app
USER hub
EXPOSE 8080
CMD ["uvicorn", "hub.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080", "--no-proxy-headers", "--no-server-header"]
