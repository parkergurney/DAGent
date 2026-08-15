FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive \
    ORCH_HARBOR_ISOLATED=1 \
    ORCH_REPO_ROOT=/app \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends git procps coreutils ca-certificates tar \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --shell /bin/bash harbor \
    && git config --global user.email harbor-dag@example.invalid \
    && git config --global user.name harbor-dag \
    && git config --system user.email harbor-dag@example.invalid \
    && git config --system user.name harbor-dag

WORKDIR /app
RUN git init -q -b main \
    && printf '# Dependency-aware canary baseline\n' > README.md \
    && git add README.md \
    && GIT_AUTHOR_DATE='2000-01-01T00:00:00Z' \
       GIT_COMMITTER_DATE='2000-01-01T00:00:00Z' \
       git commit -qm 'dependency canary baseline' \
    && chown -R harbor:harbor /app

USER harbor
