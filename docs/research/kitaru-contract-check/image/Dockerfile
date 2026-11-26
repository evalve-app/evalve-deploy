# Test agent image for the Kitaru contract check.
# It has the layout that `evalve build` requires: it starts from the Kitaru
# worker image, root owns all of /app, and the user is uid 1000.
ARG KITARU_VERSION
FROM docker.io/zenmldocker/kitaru-worker:${KITARU_VERSION}
ARG KITARU_VERSION
USER root
# The worker image has no `kitaru` CLI. An agent lockfile must add the extra.
RUN uv pip install --python /app/.venv/bin/python "kitaru[cli,worker]==${KITARU_VERSION}"
COPY probe_agent.py /app/agent/probe_agent.py
COPY launcher.py /opt/kcc/launcher.py
RUN chown -R root:root /app /opt/kcc && chmod -R go-w /app /opt/kcc
ENV PYTHONDONTWRITEBYTECODE=1
USER 1000
