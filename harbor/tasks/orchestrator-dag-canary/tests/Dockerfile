FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && git config --global user.email harbor-dag@example.invalid \
    && git config --global user.name harbor-dag

WORKDIR /app
RUN git init -q -b main \
    && printf '# Dependency-aware canary baseline\n' > README.md \
    && git add README.md \
    && GIT_AUTHOR_DATE='2000-01-01T00:00:00Z' \
       GIT_COMMITTER_DATE='2000-01-01T00:00:00Z' \
       git commit -qm 'dependency canary baseline'

COPY test.sh /tests/test.sh
COPY grader.py /tests/grader.py
RUN chmod +x /tests/test.sh

