FROM node:24-bookworm-slim AS webui-builder

WORKDIR /app
COPY webui/package.json webui/package-lock.json ./webui/
WORKDIR /app/webui
RUN npm ci
COPY webui/ ./
RUN mkdir -p /app/nanobot/web && npm run build

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates git bubblewrap openssh-client libmagic1 util-linux && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

ENV VIRTUAL_ENV=/app/.venv
ENV PATH="/app/.venv/bin:$PATH"
RUN uv venv --seed "$VIRTUAL_ENV"

ARG NANOBOT_EXTRAS=
COPY pyproject.toml README.md LICENSE THIRD_PARTY_NOTICES.md hatch_build.py ./
RUN mkdir -p nanobot && touch nanobot/__init__.py && \
    if [ -n "$NANOBOT_EXTRAS" ]; then \
        NANOBOT_SKIP_WEBUI_BUILD=1 uv pip install \
            --python "$VIRTUAL_ENV/bin/python" --no-cache ".[${NANOBOT_EXTRAS}]"; \
    else \
        NANOBOT_SKIP_WEBUI_BUILD=1 uv pip install \
            --python "$VIRTUAL_ENV/bin/python" --no-cache .; \
    fi && \
    rm -rf nanobot

COPY nanobot/ nanobot/
COPY scripts/install_channel_dependencies.py scripts/
COPY --from=webui-builder /app/nanobot/web/dist/ nanobot/web/dist/
RUN NANOBOT_SKIP_WEBUI_BUILD=1 uv pip install --python "$VIRTUAL_ENV/bin/python" --no-cache .

ARG NANOBOT_CHANNELS=whatsapp
RUN for channel in $(printf %s "$NANOBOT_CHANNELS" | tr "," " "); do \
        python -m scripts.install_channel_dependencies "$channel"; \
    done

COPY render-config.json ./

RUN useradd -m -u 1000 -s /bin/bash nanobot && \
    mkdir -p /home/nanobot/.nanobot && \
    chown -R nanobot:nanobot /home/nanobot /app/.venv && \
    chmod -R a+rX /app

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN sed -i "s/\r$//" /usr/local/bin/entrypoint.sh && chmod +x /usr/local/bin/entrypoint.sh

# Docker CLI (static binary) so nanobot can manage sibling containers on the
# host via the mounted /var/run/docker.sock.
RUN curl -fsSL https://download.docker.com/linux/static/stable/x86_64/docker-28.3.3.tgz -o /tmp/docker.tgz && \
    tar -xzf /tmp/docker.tgz -C /usr/local/bin --strip-components=1 docker/docker && \
    rm /tmp/docker.tgz

USER root
ENV HOME=/home/nanobot
ENV PYTHONUNBUFFERED=1 PYTHONFAULTHANDLER=1

EXPOSE 18790 8765

ENTRYPOINT ["entrypoint.sh"]
CMD ["status"]
