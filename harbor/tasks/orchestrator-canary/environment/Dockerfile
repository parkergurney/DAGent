FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive \
    ORCH_HARBOR_ISOLATED=1 \
    ORCH_REPO_ROOT=/app \
    ORCH_VERIFY_CMD="test \"$(cat output.txt)\" = ready"

RUN apt-get update \
    && apt-get install -y --no-install-recommends git procps coreutils ca-certificates tar \
    && rm -rf /var/lib/apt/lists/* \
    && git config --global user.email harbor-canary@example.invalid \
    && git config --global user.name harbor-canary

WORKDIR /app
RUN git init -q -b main \
    && printf 'baseline\n' > output.txt \
    && git add output.txt \
    && GIT_AUTHOR_DATE='2000-01-01T00:00:00Z' \
       GIT_COMMITTER_DATE='2000-01-01T00:00:00Z' \
       git commit -qm 'canary baseline'
