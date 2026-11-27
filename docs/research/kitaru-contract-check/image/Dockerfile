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
# With `check.py --lock`: the block of sc-534, in the form of the final layer
# of `evalve build`. uid 1000 loses the write access to the four places and to
# what is in them, and can write only below /tmp/evalve. Nothing is deleted.
ARG LOCK=0
RUN if [ "$LOCK" = "1" ]; then \
      for place in /tmp /var/tmp /run/lock /home/kitaru; do \
        if [ -e "$place" ]; then chown -R root:root "$place" && chmod -R go-w "$place" || exit 1; fi; \
      done \
      && mkdir -p /tmp/evalve && chown 1000:1000 /tmp/evalve && chmod 0700 /tmp/evalve; \
    fi
ENV PYTHONDONTWRITEBYTECODE=1
USER 1000
