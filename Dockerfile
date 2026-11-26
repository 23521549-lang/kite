# Kite's image for the Docker Compose deployment (deploy/stack): one image for every Kite service (gateway,
# workflow worker, web back end with its page, the chat's OpenCode server). They share the code and the
# state folder, and the gateway also drives Chromium (Kite's browser for kaggle.com).
#
# Built by CI on every green commit of main (ghcr.io/<owner>/kite:sha-<commit>, then :stable); kite-stack
# on the machine pulls :stable. Build here with: docker build -t kite .
ARG BASE=ubuntu:24.04

# ---------------------------------------------------------------------------- the web page (web/dist)
FROM ${BASE} AS ui
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv ca-certificates \
 && rm -rf /var/lib/apt/lists/*
# Node.js from the same wheel as the `ui` extra (no system Node, the version the page was built with).
RUN python3 -m venv /opt/node && /opt/node/bin/pip install --no-cache-dir "nodejs-wheel>=24,<25"
ENV PATH=/opt/node/bin:$PATH
WORKDIR /src/web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

# ---------------------------------------------------------------------------- binaries
FROM ${BASE} AS tools
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*
# OpenCode, the version Kite's chat was tested with, checked against its pinned SHA-512.
COPY deploy/opencode.env /tmp/opencode.env
RUN . /tmp/opencode.env \
 && curl -fsSL -o /tmp/opencode.tgz \
      "https://registry.npmjs.org/@opencode/cli-linux-x64/-/cli-linux-x64-${OPENCODE_VERSION}.tgz" \
 && echo "${OPENCODE_SHA512}  /tmp/opencode.tgz" | sha512sum -c - \
 && tar -xzf /tmp/opencode.tgz -C /tmp package/bin/opencode \
 && install -m 0755 /tmp/package/bin/opencode /usr/local/bin/opencode \
 && rm -rf /tmp/opencode.tgz /tmp/package
# The docker command alone (the workflow worker starts each task's sandbox on the host's Docker), taken
# from Ubuntu's signed docker.io package without its daemon.
RUN apt-get update \
 && cd /tmp && apt-get download docker.io \
 && dpkg-deb -x /tmp/docker.io_*.deb /tmp/docker \
 && install -m 0755 /tmp/docker/usr/bin/docker /usr/local/bin/docker \
 && rm -rf /tmp/docker.io_*.deb /tmp/docker /var/lib/apt/lists/*

# ---------------------------------------------------------------------------- Kite
FROM ${BASE} AS kite
ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright \
    OPENCODE_DISABLE_AUTOUPDATE=1 \
    PATH=/opt/kite/.venv/bin:$PATH \
    KITE_DEPLOY=compose \
    KITE_CONFIG=/var/lib/kite/kite/config.yaml \
    HOME=/var/lib/kite/home
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      python3 python3-venv ca-certificates git curl tini ripgrep libatomic1 procps tzdata openssh-client \
 && rm -rf /var/lib/apt/lists/*
# Kite runs as uid 1000 (the state folder on the host belongs to it). Ubuntu's image has a user "ubuntu"
# with that uid: it becomes "kite".
RUN if getent passwd 1000 >/dev/null; then userdel --remove "$(getent passwd 1000 | cut -d: -f1)"; fi \
 && if getent group 1000 >/dev/null; then groupdel "$(getent group 1000 | cut -d: -f1)"; fi \
 && groupadd --gid 1000 kite \
 && useradd --uid 1000 --gid 1000 --home-dir /var/lib/kite/home --no-create-home --shell /bin/bash kite \
 && mkdir -p /var/lib/kite/home /var/lib/kite/kite && chown -R 1000:1000 /var/lib/kite
COPY --from=tools /usr/local/bin/opencode /usr/local/bin/docker /usr/local/bin/

WORKDIR /opt/kite
# Libraries first: this layer changes only with pyproject.toml.
COPY pyproject.toml ./
RUN python3 -m venv .venv \
 && .venv/bin/python - > /tmp/requirements.txt <<'PY'
import tomllib
project = tomllib.load(open("pyproject.toml", "rb"))["project"]
wanted = list(project["dependencies"])
for extra in ("codemap", "kaggle", "browser", "web", "docling"):
    wanted += project["optional-dependencies"][extra]
print("\n".join(wanted))
PY
# torch CPU-only: the machine runs CPU-only, no CUDA is needed. Install first from the CPU
# index so pip sees torch already satisfied when installing docling (docling needs
# torch>=2.2.2,<3.0.0), avoiding the whole CUDA tree (~2.4 GiB of nvidia/cuda/triton wheels).
# The +cpu local version matters: a plain torch==2.15.0 pin would make pip fall back to CUDA.
RUN .venv/bin/pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu "torch==2.15.0+cpu" "torchvision==0.29.2+cpu"
RUN .venv/bin/pip install --no-cache-dir -r /tmp/requirements.txt && rm /tmp/requirements.txt
# Harness² (evaluations) at the revision the bootstrap pins (HARNESS2_REVISION in bootstrap.py; a test keeps
# the two equal), installed like on the development machine.
ARG HARNESS2=1
ARG HARNESS2_REVISION=6868817518876de8fd170c6a8354daca273b6191
RUN if [ "$HARNESS2" = 1 ]; then \
      git clone --filter=blob:none https://github.com/google-research/harness2.git /opt/harness2 \
   && git -C /opt/harness2 checkout --detach "$HARNESS2_REVISION" \
   && .venv/bin/pip install --no-cache-dir -e /opt/harness2; \
    fi
# Chromium for Kite's browser (kaggle.com), with the libraries it needs.
ARG BROWSER=1
RUN if [ "$BROWSER" = 1 ]; then \
      apt-get update \
   && .venv/bin/python -m playwright install --with-deps chromium \
   && rm -rf /var/lib/apt/lists/*; \
    fi

COPY . /opt/kite
COPY --from=ui /src/web/dist /opt/kite/web/dist
RUN .venv/bin/pip install --no-cache-dir --no-deps -e /opt/kite \
 && .venv/bin/python -m compileall -q /opt/kite/poc \
 && chmod -R a+rX /opt/kite \
 && if [ -d /opt/ms-playwright ]; then chmod -R a+rX /opt/ms-playwright; fi

# What CI built (shown in the Settings tab and by kite-stack).
ARG KITE_VERSION=""
ARG KITE_VERSION_SUBJECT=""
ARG KITE_VERSION_DATE=""
ENV KITE_VERSION=${KITE_VERSION} KITE_VERSION_SUBJECT=${KITE_VERSION_SUBJECT} KITE_VERSION_DATE=${KITE_VERSION_DATE}
LABEL org.opencontainers.image.title="kite" \
      org.opencontainers.image.description="Kite control plane: gateway, workflow worker, web, chat" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.revision="${KITE_VERSION}"

USER 1000:1000
WORKDIR /var/lib/kite/home
# -s: with the host's process space (compose.yaml: pid: host) tini is not PID 1 and must ask to reap.
ENTRYPOINT ["/usr/bin/tini", "-s", "--"]
CMD ["kite", "--help"]
