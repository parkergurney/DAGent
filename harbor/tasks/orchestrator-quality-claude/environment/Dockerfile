FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive \
    ORCH_HARBOR_ISOLATED=1 \
    ORCH_REPO_ROOT=/app \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app/repos/jsonschema:/app/repos/arrow:/app/repos/tinydb

RUN apt-get update \
    && apt-get install -y --no-install-recommends git procps coreutils tar ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --shell /bin/bash harbor \
    && git config --global user.email harbor-quality@example.invalid \
    && git config --global user.name harbor-quality \
    && git config --system user.email harbor-quality@example.invalid \
    && git config --system user.name harbor-quality

WORKDIR /app
COPY fixtures /app/repos
COPY quality_tasks.json /tmp/quality_tasks.json

RUN python3 -m pip install --no-cache-dir pytest python-dateutil 'attrs>=22.2.0' \
       'jsonschema-specifications>=2023.03.6' 'referencing>=0.25.3' 'rpds-py>=0.7.1' \
    && git init -q -b main \
    && git add repos \
    && GIT_AUTHOR_DATE='2000-01-01T00:00:00Z' \
       GIT_COMMITTER_DATE='2000-01-01T00:00:00Z' \
       git commit -qm 'semantic quality benchmark baseline' \
    && chown -R harbor:harbor /app

USER harbor
