# Image for Kite's per-task sandbox. Built by `kite sandbox build`, which copies the host's
# OpenCode binary (the version that was tested on the host) and its package cache into the
# build context, because the agent has no Internet access at run time.
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates ripgrep nodejs npm \
 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir "pytest>=8,<10"

ARG KITE_UID=1000
RUN useradd --create-home --uid ${KITE_UID} kite \
 && mkdir -p /home/kite/.config/opencode /home/kite/.local/share/opencode /home/kite/.cache /deps \
 && git config --system user.name "Kite sandbox" \
 && git config --system user.email "kite@sandbox.invalid" \
 && git config --system --add safe.directory '*'

COPY opencode-cache /home/kite/.cache/opencode
COPY opencode /usr/local/bin/opencode
RUN chmod 0755 /usr/local/bin/opencode && chown -R kite:kite /home/kite /deps

USER kite
WORKDIR /work