#!/usr/bin/env bash
# Install Kite on this machine (Ubuntu 22.04 or 24.04) with Docker Compose:
#
#   curl -fsSL https://raw.githubusercontent.com/23521549-lang/kite/main/deploy/stack/install.sh | sudo bash
#   curl -fsSL .../install.sh | sudo bash -s -- --access private --domain kite.example.com
#
# Installs Docker and cosign if needed, pulls Kite's stable image, takes kite-stack (deploy/stack/kite_stack.py) out of
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
command -v curl >/dev/null || apt-get install -y -q curl

# cosign: kite-stack runs an image only when it carries this repository's CI signature
# (KITE_VERIFY_IMAGE_SIGNATURES=1, the default of a new machine). The release CI signs with
# (sigstore/cosign-installer v3 in ci.yml), checked against its published SHA-256; a test keeps the two
# versions equal. A cosign already on the machine is kept.
COSIGN_VERSION="v2.5.2"
if ! command -v cosign >/dev/null; then
  arch="$(dpkg --print-architecture)"
  case "$arch" in
    amd64) cosign_sha256="bcfeae05557a9f313ee4392d2f335d0ff69ebbfd232019e3736fb04999fe1734" ;;
    arm64) cosign_sha256="2cbcea1873ad76274c3f241ef175d204654e3aac3e73e6ec4504e5227015cb0a" ;;
    *) echo "chưa có cosign cho kiến trúc $arch — cài cosign ${COSIGN_VERSION} bằng tay rồi chạy lại" >&2; exit 1 ;;
  esac
  echo "== cài cosign ${COSIGN_VERSION}"
  cosign_tmp="$(mktemp)"
  curl -fsSL "https://github.com/sigstore/cosign/releases/download/${COSIGN_VERSION}/cosign-linux-${arch}" -o "$cosign_tmp"
  if [[ "$(sha256sum "$cosign_tmp" | cut -d' ' -f1)" != "$cosign_sha256" ]]; then
    rm -f "$cosign_tmp"
    echo "cosign tải về không đúng SHA-256 — dừng, không cài" >&2
    exit 1
  fi
  install -m 0755 "$cosign_tmp" /usr/local/bin/cosign
  rm -f "$cosign_tmp"
fi

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
