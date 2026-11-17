#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DATA_DIR="${KITE_DATA_DIR:-/var/lib/kite}"
cd "$REPO_ROOT"

# Dry-run is intentionally local and side-effect free, including on Windows/CI.
if [[ "${KITE_BOOTSTRAP_DRY_RUN:-0}" == "1" ]]; then
  exec python3 -m poc.kaggle_worker.bootstrap --repo "$REPO_ROOT" --data-dir "$DATA_DIR" --dry-run
fi

if [[ "$(uname -s)" != "Linux" ]] || [[ ! -r /etc/os-release ]]; then
  echo "bootstrap supports Ubuntu 22.04 or 24.04 only" >&2
  exit 1
fi
# shellcheck disable=SC1091
source /etc/os-release
if [[ "${ID:-}" != "ubuntu" || ( "${VERSION_ID:-}" != "22.04" && "${VERSION_ID:-}" != "24.04" ) ]]; then
  echo "bootstrap supports Ubuntu 22.04 or 24.04 only (found ${PRETTY_NAME:-unknown})" >&2
  exit 1
fi

if [[ "$EUID" -eq 0 ]]; then
  SUDO=()
else
  command -v sudo >/dev/null || { echo "sudo is required" >&2; exit 1; }
  SUDO=(sudo)
fi

if [[ ! "$DATA_DIR" = /* ]]; then
  echo "KITE_DATA_DIR must be an absolute path on persistent storage" >&2
  exit 1
fi
if ! mountpoint -q "$DATA_DIR"; then
  echo "KITE_DATA_DIR must already be mounted on persistent storage (for example, EBS at /var/lib/kite)" >&2
  exit 1
fi
if [[ ! -w "$DATA_DIR" ]]; then
  echo "KITE_DATA_DIR must be writable by the bootstrap user" >&2
  exit 1
fi

# Install the basic tools first; the bootstrap command does not assume make,
# git, Python, or Docker were preinstalled on a fresh Ubuntu image.
"${SUDO[@]}" apt-get update
"${SUDO[@]}" env DEBIAN_FRONTEND=noninteractive apt-get install -y \
  ca-certificates curl gnupg git make python3-minimal

# Configure Docker's official Ubuntu repository idempotently.
"${SUDO[@]}" install -m 0755 -d /etc/apt/keyrings
if [[ ! -s /etc/apt/keyrings/docker.asc ]]; then
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg | "${SUDO[@]}" tee /etc/apt/keyrings/docker.asc >/dev/null
  "${SUDO[@]}" chmod a+r /etc/apt/keyrings/docker.asc
fi
ARCH="$(dpkg --print-architecture)"
UBUNTU_CODENAME="${UBUNTU_CODENAME:-${VERSION_CODENAME:-}}"
if [[ -z "$UBUNTU_CODENAME" ]]; then
  echo "could not determine Ubuntu codename for Docker repository" >&2
  exit 1
fi
SOURCE_FILE="/etc/apt/sources.list.d/docker.sources"
if [[ ! -f "$SOURCE_FILE" ]] || ! "${SUDO[@]}" grep -q "^Suites: ${UBUNTU_CODENAME}$" "$SOURCE_FILE"; then
  SOURCE_TMP="$(mktemp)"
  trap 'rm -f "$SOURCE_TMP"' EXIT
  cat >"$SOURCE_TMP" <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: ${UBUNTU_CODENAME}
Components: stable
Architectures: ${ARCH}
Signed-By: /etc/apt/keyrings/docker.asc
EOF
  "${SUDO[@]}" install -m 0644 "$SOURCE_TMP" "$SOURCE_FILE"
fi
"${SUDO[@]}" apt-get update
"${SUDO[@]}" env DEBIAN_FRONTEND=noninteractive apt-get install -y \
  docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
"${SUDO[@]}" systemctl enable --now docker
"${SUDO[@]}" docker compose version >/dev/null

# Install a pinned uv release and its managed Python 3.12 runtime.
UV_BIN="$(command -v uv || true)"
if [[ -z "$UV_BIN" ]]; then
  UV_INSTALL_DIR="$HOME/.local/bin" UV_NO_MODIFY_PATH=1 \
    sh -c 'curl -LsSf https://astral.sh/uv/0.13.7/install.sh | sh'
  UV_BIN="$HOME/.local/bin/uv"
fi
export PATH="$(dirname "$UV_BIN"):$PATH"
"$UV_BIN" python install 3.12
export KITE_BOOTSTRAP_PYTHON="$("$UV_BIN" python find 3.12)"

if ! df -Pk "$DATA_DIR" | awk 'NR==2 { exit !($4 >= 20 * 1024 * 1024) }'; then
  echo "at least 20 GiB free space is required on KITE_DATA_DIR" >&2
  exit 1
fi

exec "$KITE_BOOTSTRAP_PYTHON" -m poc.kaggle_worker.bootstrap --repo "$REPO_ROOT" --data-dir "$DATA_DIR"
