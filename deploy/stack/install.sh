#!/usr/bin/env bash
# Install Kite on this machine (Ubuntu 22.04 or 24.04) with Docker Compose:
#
#   curl -fsSL https://raw.githubusercontent.com/23521549-lang/kite/main/deploy/stack/install.sh | sudo bash
#   curl -fsSL .../install.sh | sudo bash -s -- --access private --domain kite.example.com
#
# Installs Docker if needed, pulls Kite's stable image, takes kite-stack (deploy/stack/kite_stack.py) out of
# that image and lets it do the rest: secrets made on this machine, default config, every service started
# and checked, automatic updates. Safe to run again (it repairs, keeps data and secrets).
# Every option of `kite-stack install --help` can be passed; /var/lib/kite should be a disk of its own.
set -euo pipefail

IMAGE="ghcr.io/23521549-lang/kite"
CHANNEL="stable"
TOKEN_FILE=""
args=("$@")
while [[ $# -gt 0 ]]; do
  case "$1" in
    --image) IMAGE="$2"; shift 2 ;;
    --image=*) IMAGE="${1#*=}"; shift ;;
    --channel) CHANNEL="$2"; shift 2 ;;
    --channel=*) CHANNEL="${1#*=}"; shift ;;
    --ghcr-token-file) TOKEN_FILE="$2"; shift 2 ;;
    --ghcr-token-file=*) TOKEN_FILE="${1#*=}"; shift ;;
    *) shift ;;
  esac
done

if [[ "$EUID" -ne 0 ]]; then
  echo "chạy bằng root: curl ... | sudo bash" >&2
  exit 1
fi
if [[ -r /etc/os-release ]]; then
  # shellcheck disable=SC1091
  . /etc/os-release
fi
if [[ "${ID:-}" != "ubuntu" ]]; then
  echo "Kite cài trên Ubuntu 22.04 hoặc 24.04 (máy này: ${PRETTY_NAME:-không rõ})" >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
if ! command -v docker >/dev/null || ! docker compose version >/dev/null 2>&1; then
  echo "== cài Docker"
  apt-get update -q
  apt-get install -y -q ca-certificates curl gnupg python3
  install -m 0755 -d /etc/apt/keyrings
  if [[ ! -s /etc/apt/keyrings/docker.asc ]]; then
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
  fi
  docker_key_fingerprint="$(gpg --show-keys --with-colons /etc/apt/keyrings/docker.asc 2>/dev/null | awk -F: '/^fpr:/ {print $10; exit}')"
  if [[ "$docker_key_fingerprint" != "9DC858229FC7DD38854AE2D88D81803C0EBFCD88" ]]; then
    echo "khóa ký của Docker không đúng dấu vân tay (thấy: ${docker_key_fingerprint:-không đọc được}) — dừng, không cài" >&2
    exit 1
  fi
  cat >/etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: ${UBUNTU_CODENAME:-${VERSION_CODENAME}}
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
  apt-get update -q
  apt-get install -y -q docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  systemctl enable --now docker
fi
command -v python3 >/dev/null || apt-get install -y -q python3

mkdir -p /var/lib/kite
if [[ -n "$TOKEN_FILE" ]]; then
  docker login ghcr.io -u "$(echo "$IMAGE" | cut -d/ -f2)" --password-stdin <"$TOKEN_FILE"
fi

echo "== tải ${IMAGE}:${CHANNEL}"
docker pull "${IMAGE}:${CHANNEL}"
work="$(mktemp -d)"
trap 'rm -rf "$work"; docker rm -f kite-stack-install >/dev/null 2>&1 || true' EXIT
docker rm -f kite-stack-install >/dev/null 2>&1 || true
docker create --name kite-stack-install "${IMAGE}:${CHANNEL}" >/dev/null
docker cp kite-stack-install:/opt/kite/deploy/stack/kite_stack.py "$work/kite_stack.py"

echo "== kite-stack install"
python3 "$work/kite_stack.py" install "${args[@]}"
